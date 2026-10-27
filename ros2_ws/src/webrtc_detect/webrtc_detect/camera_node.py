#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
camera_node —— 把板子上的摄像头变成 ROS 话题

    v4l2src → [jpegdec / mppjpegdec] → videoconvert → BGR → appsink
                                              │
                                   采集线程（本文件）
                                              │
                         sensor_msgs/Image  /camera/image_raw

配套的话题（切源）和 monitor 模式里"页面上点按钮"是同一件事，只是这里走
ROS：

    /camera/select   std_msgs/String   请求换一路（"mipi" / "usb"）
    /camera/source   std_msgs/String   **回报当前真正在出的那一路**（latched）

为什么 source 要 latched（TRANSIENT_LOCAL）：device_agent 可能比本节点晚起，
它一起来就要知道"现在用的是哪一路"；普通 topic 上它得等到下一次切换才知道。

为什么切源要回报、而不是发布完就当切好了：发布一条消息谁都会成功，
真正能证明"设备已经重开、在出新的一路画面"的，只有本节点在**拉到第一帧
之后**发的那条 /camera/source。device_agent 的 HTTP 响应就等这条。

画质兜底（亮度闭环 + 白平衡）在**打开设备的时候**做，和 monitor 模式
在 device_agent 里做的是同一套代码（src/camera.py、src/whitebalance.py）：
板子上没有 rkaiq 3A，MIPI 这路的 AE/AWB 都得软件兜底。USB 那一路自带 3A，
整段跳过。

依赖仓库的 src/ 目录（sources.py / camera.py / whitebalance.py / pipeline.py）。
tools/run-ros-nodes.sh 和 launch/detect.launch.py 都会把仓库的 src/ 加进
PYTHONPATH；手工 `ros2 run` 时要自己带。
"""

import logging
import os
import threading
from types import SimpleNamespace

import rclpy
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from sensor_msgs.msg import Image
from std_msgs.msg import String

import pipeline as P          # 必须早于 Gst.init()：清掉 BSP 埋的 GST_V4L2_USE_LIBV4L2
import sources
import whitebalance
from .detector_core import (ColorBalance, MJPEG_DECODER_DEFAULT, MJPEG_DECODERS,
                            as_uint8_array, frame_stride, require_numpy,
                            segment_desc)

log = logging.getLogger("camera_node")

#: 拉帧轮询超时。够短，切源/退出时线程能及时响应，不会卡在等帧上。
POLL_NS = 200 * 1000 * 1000
#: 一直拉不到帧就重开采集（设备掉线/CSI 链路抖动时常见）。
IDLE_GIVEUP_NS = 5 * 1000 * 1000 * 1000

_Gst = None


def require_gst():
    global _Gst
    if _Gst is None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)          # 必须在 import pipeline 之后（它清过环境变量）
        _Gst = Gst
    return _Gst


class Capture:
    """一段采集。**只由采集线程创建/销毁**（GStreamer 状态机是单线程的）。"""

    def __init__(self, a, src):
        Gst = require_gst()
        # GstApp 必须在 parse 之前 import，否则 get_by_name 拿到的是个没有
        # try_pull_sample 的秃 Element（PyGObject 按类型缓存包装对象）。
        whitebalance.require_app_sink()

        decoder = getattr(a, "decoder", MJPEG_DECODER_DEFAULT)
        desc = segment_desc(a, src, decoder)
        self.pipeline = Gst.Pipeline.new("ros-camera-%s" % src.name)
        if self.pipeline is None:
            raise RuntimeError("建不出采集 pipeline（源 %s）" % src.name)
        self.bin = Gst.parse_bin_from_description(desc, False)
        if self.bin is None:
            raise RuntimeError("采集段建不出来（源 %s）：%s" % (src.name, desc))
        self.pipeline.add(self.bin)
        self.sink = self.pipeline.get_by_name("capsink")
        if self.sink is None or not hasattr(self.sink, "try_pull_sample"):
            raise RuntimeError("capsink 不是 GstAppSink（GstApp 没在 parse 之前 import？）")
        self.bus = self.pipeline.get_bus()
        self.src = src
        self.reported = False        # 是否已经回报过 /camera/source

        # 白平衡只对 raw 源（MIPI）做：USB 自带 AWB，对它校正只会把颜色推歪。
        self.wb = None
        if src.kind == "raw" and getattr(a, "camera_wb", None):
            self.wb = ColorBalance(a.camera_wb)
            log.info("%s：白平衡 %s（只对 raw 源）", src.name, self.wb.desc)

    def start(self):
        Gst = require_gst()
        self.pipeline.set_state(Gst.State.PLAYING)

    def pull(self, timeout_ns):
        return self.sink.try_pull_sample(timeout_ns)

    def poll_bus(self):
        """把总线上的错误打出来。

        本进程没有 GLib 主循环可跑（主线程在 rclpy.spin），所以不用
        add_signal_watch，直接在采集循环里 pop —— 有帧的时候总线是空的，
        这个调用很便宜。
        """
        Gst = require_gst()
        while True:
            msg = self.bus.pop_filtered(
                Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is None:
                return
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                log.error("%s：GStreamer 错误: %s", self.src.name, err.message)
                if debug:
                    log.error("  调试信息: %s", debug)
            else:
                log.warning("%s：采集走到 EOS（摄像头被拔掉？）", self.src.name)

    def stop(self):
        Gst = require_gst()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_state(2 * Gst.SECOND)
        self.pipeline = None
        self.sink = None
        self.wb = None


class CameraNode(Node):
    """采集线程 + 一个 ROS 节点。ROS 回调只改"请求"，GStreamer 只碰在采集线程。"""

    def __init__(self):
        super().__init__("camera_node")

        self.declare_parameter("camera_source", sources.SOURCES[0].name)
        self.declare_parameter("width", 1280)
        self.declare_parameter("height", 720)
        self.declare_parameter("fps", 30)
        self.declare_parameter("image_topic", "/camera/image_raw")
        self.declare_parameter("select_topic", "/camera/select")
        self.declare_parameter("source_topic", "/camera/source")
        self.declare_parameter("frame_id", "camera")
        self.declare_parameter("camera_wb", "")        # 空=不校正；auto 或 "R,B"
        self.declare_parameter("camera_decoder", MJPEG_DECODER_DEFAULT)  # sw/mpp
        self.declare_parameter("camera_ctl", "")
        self.declare_parameter("camera_no_auto", False)
        self.declare_parameter("camera_target_y", 110.0)
        self.declare_parameter("camera_exposure", 1648)
        self.declare_parameter("camera_probe", "/dev/video12")

        gp = self.get_parameter
        self.a = SimpleNamespace(
            width=int(gp("width").value),
            height=int(gp("height").value),
            fps=int(gp("fps").value),
            camera=None,
            camera_wb=(gp("camera_wb").value or None),
            decoder=str(gp("camera_decoder").value or MJPEG_DECODER_DEFAULT),
            camera_ctl=(gp("camera_ctl").value or None),
            camera_no_auto=bool(gp("camera_no_auto").value),
            camera_target_y=float(gp("camera_target_y").value),
            camera_exposure=int(gp("camera_exposure").value),
            camera_probe=(gp("camera_probe").value or None),
        )
        # 白平衡的取值先在启动时验掉：真到打开设备时才发现写错了，报错会
        # 夹在"设备打不开"一堆日志中间。
        if self.a.camera_wb:
            try:
                whitebalance.parse_spec(self.a.camera_wb)
            except ValueError as exc:
                self.get_logger().error("%s —— 不启用白平衡" % exc)
                self.a.camera_wb = None
        # 解码器名写错要在启动时说，而不是等打开设备时才崩在 parse_bin 里。
        if self.a.decoder not in MJPEG_DECODERS:
            self.get_logger().error(
                "camera_decoder=%r 不认识，退回 %s（可选：%s）"
                % (self.a.decoder, MJPEG_DECODER_DEFAULT,
                   ", ".join(MJPEG_DECODERS)))
            self.a.decoder = MJPEG_DECODER_DEFAULT

        self.frame_id = str(gp("frame_id").value)
        self._source = sources.initial(str(gp("camera_source").value))
        self._request = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

        self._qos_latched = QoSProfile(
            depth=1, history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._pub_image = self.create_publisher(
            Image, str(gp("image_topic").value), 10)
        self._pub_source = self.create_publisher(
            String, str(gp("source_topic").value), self._qos_latched)
        self._sub_select = self.create_subscription(
            String, str(gp("select_topic").value), self._on_select, 10)

        # 读这些数的是 get_logger/外部脚本；写的是采集线程。普通 int。
        self.frames = 0
        self.errors = 0

        self._thread = threading.Thread(
            target=self._run, name="camera-capture", daemon=True)
        self._thread.start()

        # rclpy 的 logger 不吃 %-占位符 + 位置参数（多传一个就是 TypeError），
        # 所有 get_logger() 调用都先自己 %-格式化好再传一个字符串。
        self.get_logger().info(
            "camera_node：%s %dx%d@%d → %s（切源 %s，回报 %s）"
            % (self._source.name, self.a.width, self.a.height, self.a.fps,
               gp("image_topic").value, gp("select_topic").value,
               gp("source_topic").value))
        if self.a.camera_wb:
            self.get_logger().info("  白平衡：%s（只对 raw 源生效）"
                                   % self.a.camera_wb)
        if self._source.kind == "mjpeg":
            self.get_logger().info("  MJPG 解码：%s" % self.a.decoder)

    # ---------------- ROS 回调（在 rclpy spin 线程） ----------------
    def _on_select(self, msg):
        name = (msg.data or "").strip()
        try:
            src = sources.resolve(name)
        except KeyError as exc:
            self.get_logger().warn("忽略切源请求：%s" % sources.message_of(exc))
            return
        if not os.path.exists(src.device):
            # 不把请求排进队列：设备不在时采集线程会一直等它出现，而
            # device_agent 那边等的是"回报成这一路"，会白白拖到超时。
            self.get_logger().warn(
                "忽略切源请求 %s：设备 %s 不存在（摄像头没插好？）"
                % (src.name, src.device))
            return
        with self._lock:
            current = self._source.name if self._request is None else self._request.name
        if src.name == current:
            self.get_logger().info("切源请求 %s：已经在这一路上" % src.name)
            return
        self.get_logger().info("收到切源请求：%s（%s）" % (src.name, src.device))
        with self._lock:
            self._request = src

    # ---------------- 采集线程 ----------------
    def _wanted(self):
        """取下一个要用的源；没有新请求就返回 None。"""
        with self._lock:
            src = self._request
            self._request = None
            if src is not None:
                self._source = src
            return src

    def _has_request(self):
        with self._lock:
            return self._request is not None

    def _run(self):
        while not self._stop.is_set():
            switched = self._wanted()
            if switched is not None:
                self.get_logger().info("切到 %s（%s）"
                                       % (switched.name, switched.device))

            src = self._source
            if not os.path.exists(src.device):
                self.get_logger().warn(
                    "设备 %s 不存在（%s），2 秒后重试" % (src.device, src.name))
                self._sleep(2.0)
                continue

            cap = None
            try:
                # 画质兜底在打开设备**之前**跑：亮度闭环要独占探测节点，
                # 此刻旧采集已经停了（调到这里时上一轮已经 cap.stop()）。
                sources.apply_quality(src, self.a, log)
                cap = Capture(self.a, src)
                cap.start()
                self.get_logger().info("采集已启动：%s（%s）"
                                       % (src.name, src.device))
            except Exception as exc:         # noqa: BLE001
                self.errors += 1
                self.get_logger().error("打开 %s 失败：%s（2 秒后重试）"
                                        % (src.name, exc))
                if cap is not None:
                    cap.stop()
                self._sleep(2.0)
                continue

            try:
                self._capture_loop(cap)
            finally:
                cap.stop()
                self.get_logger().info("采集已停止：%s" % src.name)

    def _capture_loop(self, cap):
        idle = 0
        while not self._stop.is_set():
            if self._has_request():
                return                       # 有新请求：出去重开采集
            sample = cap.pull(POLL_NS)
            if sample is None:
                cap.poll_bus()
                idle += 1
                if idle * POLL_NS > IDLE_GIVEUP_NS:
                    self.get_logger().warn(
                        "%s 连续 %d 秒没有画面，重开采集"
                        % (cap.src.name, IDLE_GIVEUP_NS // (1000 * 1000 * 1000)))
                    return
                continue
            idle = 0

            frame = self._to_bgr(sample)
            if frame is None:
                self.errors += 1
                continue
            if cap.wb is not None:
                try:
                    frame = cap.wb.apply(frame)
                except Exception as exc:     # noqa: BLE001
                    self.errors += 1
                    self.get_logger().warn("白平衡处理失败，停用它：%s" % exc)
                    cap.wb = None

            self._publish(frame)
            if not cap.reported:
                # 第一帧出来了才算"真的在这一路上"—— 设备打开成功但没画面
                # （CSI 链路坏、格式不对）时，回报成切换成功就是在骗人。
                cap.reported = True
                self._report_source(cap.src)

    def _to_bgr(self, sample):
        """appsink 的 sample → HxWx3 BGR（**拷贝**，unmap 之后原 buffer 无效）。"""
        Gst = require_gst()
        np = require_numpy()
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if caps is None or caps.get_size() < 1:
            self.get_logger().warn("appsink 没有 caps")
            return None
        s = caps.get_structure(0)
        fmt = s.get_value("format") if s.has_field("format") else None
        if fmt != "BGR":
            self.get_logger().warn("采集帧应为 BGR，实际 %r" % (fmt,))
            return None
        width = int(s.get_value("width"))
        height = int(s.get_value("height"))

        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            self.get_logger().warn("buffer 映射失败")
            return None
        try:
            raw = np.frombuffer(info.data, dtype=np.uint8)
            stride = frame_stride(width, height, raw.size)
            if raw.size < stride * height:
                self.get_logger().warn(
                    "BGR buffer 尺寸不对：%d 字节，期望至少 %d（stride=%d）"
                    % (raw.size, stride * height, stride))
                return None
            return (raw[:stride * height].reshape(height, stride)
                    [:, :width * 3].reshape(height, width, 3).copy())
        finally:
            buf.unmap(info)

    def _publish(self, frame):
        np = require_numpy()
        h, w = int(frame.shape[0]), int(frame.shape[1])
        msg = Image()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id
        msg.height = h
        msg.width = w
        msg.encoding = "bgr8"
        msg.is_bigendian = False
        msg.step = w * 3
        # as_uint8_array 而不是 tobytes()：bytes 赋给 uint8[] 字段会走逐元素
        # 转换，720p 一帧 439 ms（见 detector_core.as_uint8_array）。
        msg.data = as_uint8_array(frame)
        self._pub_image.publish(msg)
        self.frames += 1

    def _report_source(self, src):
        self._pub_source.publish(String(data=src.name))
        self.get_logger().info("已回报当前源：%s" % src.name)

    def _sleep(self, seconds):
        self._stop.wait(seconds)

    # ---------------- 收尾 ----------------
    def shutdown(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(5.0)
            if thread.is_alive():
                self.get_logger().warn("采集线程 5 秒内没停下")


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    rclpy.init(args=None)
    node = None
    try:
        node = CameraNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.shutdown()
            node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:                    # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
