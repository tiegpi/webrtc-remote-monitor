#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ros_mode.py —— ROS 2 模式：图像走话题总线，WebRTC 只做"订阅 + 推流"

和 detect 模式（采集 → OpenCV → RKNN → 画框 → 推流，全在一个进程里）相比，
这条链把**图像源和推理都搬到了 ROS 节点里**：

    camera_node ──/camera/image_raw──> detector_node ──/detect/image_annotated──┐
         │                                  │                                   │
         │ /camera/source（回报当前源）       │ /detect/persons（JSON）            │
         │ <──/camera/select（切源请求）       │                                   │
         │                                  │                                   ▼
         └────────────────────────────── 本进程（device_agent --mode ros）
                                          RosLink（切换/回报）
                                          RosBridge（订阅标注图 → appsrc）
                                            → videoconvert → mpph264enc → webrtcbin

这样推理是一个**普通 ROS 节点**，规划/控制等其它节点可以直接订阅
`/detect/persons` 使用检测结果；WebRTC 这一层只负责"把话题上的画面推给
浏览器"，和 monitor 模式的骨架（ICE、音频、RTP 尾段、会话）完全同源。

三个对象的分工：

    RosLink    整个 agent 一个：rclpy 节点 + 一个 spin 线程，负责
               /camera/select 发布、/camera/source 与 /detect/persons 订阅。
    RosBridge  每路观看端一个：把 /detect/image_annotated 上的 Image
               喂给本会话骨架里的 appsrc。
    RosSwitch  每路观看端一个：接口和 pipeline.VideoSwitch 对齐
               （source / attach_first / switch / start_bridges / dispose /
               n_switches / overlay），session.py 不需要知道自己拿的是哪一种。

**为什么拆成"一个长驻节点 + 每会话一个订阅"**：

  · 没有观看端时不应该为图像做任何拷贝 —— 订阅是观看端接入时才建的；
  · 切源、读回报在没有会话时也要能用（页面上的按钮一直在），所以节点必须
    活在整个进程的寿命里，不能跟着会话建/拆；
  · rclpy.shutdown() 是**进程级**的。如果每次拆会话都调它，下一个观看端
    接入时 rclpy 已经死了，而报错会出现在一个和"上一次观看端离开"看起来
    毫无关系的地方 —— 所以这里只在进程退出时 shutdown 一次。

线程归属（和项目里其它跨线程代码同一个约定）：

  · Gst 元素只在 GLib 线程碰 —— RosSwitch.dispose() 由 session 投递过去；
  · 图像回调、订阅的建/拆都在 ros-spin 线程；appsrc.push_buffer 本身线程安全；
  · 计数器是普通 int/float，读的人在 1Hz 遥测线程，GIL 下不加锁。

**前提**：本进程要和节点在同一个 ROS_DOMAIN，并且 camera_node 发布的分辨率
必须和 --width/--height 一致（appsrc 的 caps 是写死的，不一致会逐帧被丢掉，
/stats 里表现为 ros_frames 不涨、ros_errors 每 100 帧一行）。
"""

import logging
import threading
import time

import pipeline as P
import sources
import whitebalance

log = logging.getLogger("ros")

#: 默认话题。detector_node 把框烧进画面之后才发布标注图 —— 预览端看到的
#: 就是"ROS 总线上的最终画面"，而不是再叠一层。
IMAGE_TOPIC_DEFAULT = "/detect/image_annotated"
PERSONS_TOPIC_DEFAULT = "/detect/persons"
SELECT_TOPIC_DEFAULT = "/camera/select"
SOURCE_TOPIC_DEFAULT = "/camera/source"

#: 接受哪些 sensor_msgs/Image 编码。usb_cam 之类的节点默认发 rgb8，
#: 所以这里顺手支持；其它格式（yuv422、mono8…）不猜，直接报错。
ENCODINGS = ("bgr8", "rgb8")

#: 切源之后等 camera_node 回报上限。它要拆旧管线、跑亮度闭环（MIPI 那一路
#: 会 sleep 好几轮）、再开新管线，几秒钟是正常的。
SWITCH_TIMEOUT = 8.0

_np = None
_Gst = None


# --------------------------------------------------------------------------- #
# 惰性依赖
# --------------------------------------------------------------------------- #
def require_numpy():
    global _np
    if _np is None:
        try:
            import numpy
        except ImportError:
            raise RuntimeError(
                "缺少 numpy —— ROS 模式要用它接 sensor_msgs/Image\n"
                "  板子:  sudo apt install python3-numpy") from None
        _np = numpy
    return _np


def require_gst():
    global _Gst
    if _Gst is None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        _Gst = Gst
    return _Gst


def require_rclpy():
    """导入 rclpy，缺环境时给一句人话（而不是 ImportError 栈）。

    rclpy 在 /opt/ros/<distro>/lib/python3.x/site-packages 里，
    **没 source setup.bash 时普通 python3 是 import 不到的**，而那种报错
    看起来像是"没装 ROS"，实际只是少了一行 source。
    """
    try:
        import rclpy
    except ImportError as exc:
        raise RuntimeError(
            "缺少 rclpy —— ROS 模式必须在 ROS 2 环境里启动\n"
            "  先 source /opt/ros/humble/setup.bash"
            "（tools/run-agent.sh 在 MODE=ros 时会替你 source）\n"
            "  原始报错: %s" % exc) from None
    return rclpy


def check_available():
    """启动前检查（和 detect_mode.check_available 同款）。返回错误串或 None。"""
    try:
        require_rclpy()
    except RuntimeError as exc:
        return str(exc)
    try:
        import sensor_msgs.msg  # noqa: F401
        import std_msgs.msg     # noqa: F401
    except ImportError as exc:
        return ("ROS 环境里缺 sensor_msgs/std_msgs：%s\n"
                "  正常安装的 ros-humble-ros-base 都带这两个包" % exc)
    try:
        require_numpy()
    except RuntimeError as exc:
        return str(exc)
    return None


# --------------------------------------------------------------------------- #
# 纯 numpy：sensor_msgs/Image → BGR 帧（宿主机可单测，不需要 rclpy）
# --------------------------------------------------------------------------- #
def image_to_bgr(data, width, height, step, encoding="bgr8"):
    """把 Image 的 data 变成 HxWx3 的 BGR 数组；返回 (frame, 错误说明)。

    **`step` 不一定是 width*3。** ROS 只在"行尾有对齐填充"时才让它不等，
    但真出现时按 width*3 去 reshape 会把整幅图切成斜的 —— 症状是画面像被
    撕开一样错位，而不是报错。所以这里按 step 先切行再取有效列。

    rgb8 顺手支持（usb_cam 默认就是它）：翻转通道得到一个**新数组**，
    下面的 ascontiguousarray 会保证它是连续内存，可以直接交给 GStreamer。
    """
    np = require_numpy()
    if encoding not in ENCODINGS:
        return None, "不认识的图像编码 %r（只认 %s）" % (
            encoding, "、".join(ENCODINGS))
    width, height, step = int(width), int(height), int(step)
    if width <= 0 or height <= 0:
        return None, "图像尺寸非法: %dx%d" % (width, height)
    row = width * 3
    if step < row:
        return None, "step=%d 小于一行所需 %d 字节（%s）" % (step, row, encoding)

    try:
        raw = np.frombuffer(data, dtype=np.uint8)
    except (TypeError, ValueError) as exc:
        return None, "图像数据不是字节序列: %s" % exc
    need = height * step
    if raw.size < need:
        return None, "图像数据不够：%d 字节，期望 %d（step=%d）" % (
            raw.size, need, step)

    rows = raw[:need].reshape(height, step)[:, :row].reshape(height, width, 3)
    if encoding == "rgb8":
        rows = rows[..., ::-1]
    return np.ascontiguousarray(rows), None


# --------------------------------------------------------------------------- #
# 长驻的 rclpy 节点：切源 + 回报
# --------------------------------------------------------------------------- #
class RosLink:
    """整个 device_agent 共用的一个 ROS 节点。

    **只由 device_agent.main() 创建和停止。** 它活得比会话长：页面在没有
    观看端时也能切摄像头，靠的就是这里的发布器还在。
    """

    def __init__(self, args, log_=None):
        rclpy = require_rclpy()
        from rclpy.node import Node
        from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                               ReliabilityPolicy)
        from std_msgs.msg import String

        self.log = log_ or log
        self._rclpy = rclpy
        self._String = String
        self.node = Node("webrtc_ros_bridge")
        self._lock = threading.Lock()

        # /camera/source 用 latched（TRANSIENT_LOCAL + RELIABLE）：agent 比
        # camera_node 晚起时也要能立刻知道当前是哪一路，而不是等下一次切换。
        self._latched = QoSProfile(
            depth=1, history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL)

        self._pub_select = self.node.create_publisher(
            String, args.ros_select_topic, 10)
        self._sub_source = self.node.create_subscription(
            String, args.ros_source_topic, self._on_source, self._latched)
        self._sub_persons = self.node.create_subscription(
            String, args.ros_persons_topic, self._on_persons, 10)

        # 回调线程写、遥测/HTTP 线程读。普通 str/float，GIL 下足够。
        self._source = ""
        self._source_at = 0.0
        self._persons = 0
        self._persons_at = 0.0
        self._persons_n = 0
        self._persons_ms = 0.0

        self._stop = threading.Event()
        self._thread = None

    # ---------------- 生命周期 ----------------
    def start(self):
        self._thread = threading.Thread(
            target=self._spin, name="ros-spin", daemon=True)
        self._thread.start()
        self.log.info("ROS 节点 %s 已启动（切源 %s，回报 %s，检测 %s）",
                      self.node.get_name(), self._select_topic(),
                      self._source_topic(), self._persons_topic())

    def stop(self, timeout=3.0):
        """收 spin 线程并关掉 rclpy。**只在进程退出时调。**"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                self.log.warning("ROS spin 线程 %.1fs 内没停下", timeout)
        try:
            self.node.destroy_node()
        except Exception:                    # noqa: BLE001
            self.log.debug("销毁 ROS 节点时出错（忽略）", exc_info=True)
        try:
            self._rclpy.shutdown()
        except Exception:                    # noqa: BLE001
            self.log.debug("关闭 rclpy 时出错（忽略）", exc_info=True)

    def _spin(self):
        from rclpy.executors import SingleThreadedExecutor

        ex = SingleThreadedExecutor()
        ex.add_node(self.node)
        try:
            # 手写 spin_once 而不是 executor.spin()：spin() 只在 rclpy 被
            # shutdown 时才返回，而这里要能在**进程还活着**的时候停线程。
            while not self._stop.is_set() and self._rclpy.ok():
                try:
                    ex.spin_once(timeout_sec=0.2)
                except Exception:            # noqa: BLE001
                    if self._stop.is_set():
                        break
                    self.log.exception("ROS spin 出错，线程继续")
        finally:
            ex.remove_node(self.node)

    # ---------------- 话题名（日志用） ----------------
    def _select_topic(self):
        return self._pub_select.topic_name

    def _source_topic(self):
        return self._sub_source.topic_name

    def _persons_topic(self):
        return self._sub_persons.topic_name

    # ---------------- 回调 ----------------
    def _on_source(self, msg):
        with self._lock:
            self._source = (msg.data or "").strip()
            self._source_at = time.monotonic()

    def _on_persons(self, msg):
        """detector_node 每次推理发一条 JSON。这里只取"几个人 / 多少毫秒"。"""
        import json

        try:
            payload = json.loads(msg.data or "{}")
        except (TypeError, ValueError):
            payload = {}
        n = payload.get("count")
        ms = payload.get("inference_ms")
        with self._lock:
            self._persons += 1
            self._persons_at = time.monotonic()
            if isinstance(n, int):
                self._persons_n = n
            if isinstance(ms, (int, float)):
                self._persons_ms = float(ms)

    # ---------------- 对外 ----------------
    def select(self, name):
        """请求 camera_node 换一路。**不阻塞**，回报由 wait_source 去等。"""
        self._pub_select.publish(self._String(data=name))
        self.log.info("已发布切源请求 %s → %s", self._select_topic(), name)

    def current_source(self):
        """camera_node 最近一次回报的源名；还没收到就是 None。"""
        with self._lock:
            return self._source or None

    def wait_source(self, name, timeout=SWITCH_TIMEOUT):
        """等 camera_node 把当前源回报成 name。返回 (是否等到, 当前回报值)。

        页面上的切换要一个**设备侧的证据**：发布一条 select 谁都会，
        camera_node 有没有真的重开设备、开成哪一路，只有它自己的回报算数。
        """
        deadline = time.monotonic() + float(timeout)
        while True:
            got = self.current_source()
            if got == name:
                return True, got
            if time.monotonic() >= deadline:
                return False, got
            time.sleep(0.05)

    def persons(self):
        with self._lock:
            return {
                "count": self._persons_n,
                "inference_ms": round(self._persons_ms, 1),
                "messages": self._persons,
                "age_ms": (round((time.monotonic() - self._persons_at) * 1000, 1)
                           if self._persons_at else None),
            }

    def status(self):
        """给 GET /stats 用的一句话状态（不需要会话）。"""
        with self._lock:
            source = self._source or None
            age = (round((time.monotonic() - self._source_at) * 1000, 1)
                   if self._source_at else None)
        return {
            "node": self.node.get_name(),
            "select_topic": self._select_topic(),
            "source_topic": self._source_topic(),
            "persons_topic": self._persons_topic(),
            "source": source,
            "source_age_ms": age,
            "persons": self.persons(),
        }

    # ---------------- 每会话的图像订阅 ----------------
    def subscribe_image(self, topic, callback):
        """在长驻节点上挂一个图像订阅（观看端接入时）。"""
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import Image

        return self.node.create_subscription(
            Image, topic, callback, qos_profile_sensor_data)

    def unsubscribe_image(self, sub):
        if sub is None:
            return
        try:
            self.node.destroy_subscription(sub)
        except Exception:                    # noqa: BLE001
            self.log.debug("摘图像订阅时出错（忽略）", exc_info=True)


# --------------------------------------------------------------------------- #
# 每会话的桥：/detect/image_annotated → appsrc
# --------------------------------------------------------------------------- #
class RosBridge:
    """把话题上的标注图推进本会话骨架里的 appsrc。

    每路观看端一个。订阅在**观看端接入之后**才建：没有人在看的时候，
    图像在 detector_node 那边就已经有人消费（推流不需要再拷一份 720p）。
    """

    def __init__(self, link, appsrc, a, log_):
        self.link = link
        self.appsrc = appsrc
        self.a = a
        self.log = log_
        self.width = int(a.width)
        self.height = int(a.height)
        self._sub = None
        self._running = False
        self._lock = threading.Lock()

        # 读这些数的是 1Hz 遥测线程；写的是 ros-spin 线程。普通 int/float。
        self.frames = 0
        self.errors = 0
        self.last_error = ""
        self.last_at = 0.0

    # ---------------- 生命周期 ----------------
    def start(self):
        with self._lock:
            self._running = True
        self._sub = self.link.subscribe_image(self.a.ros_image_topic, self._on_image)
        self.log.info("已订阅 %s（期望 %dx%d bgr8），等待 detector_node 的画面",
                      self.a.ros_image_topic, self.width, self.height)

    def stop(self):
        """先停回调，再摘订阅：反过来的话摘的瞬间可能正有一个回调在跑，
        它会往一个即将被 NULL 掉的 appsrc 上推。"""
        with self._lock:
            self._running = False
            self.appsrc = None       # 回调拿到的是 None 就直接返回
        self.link.unsubscribe_image(self._sub)
        self._sub = None

    # ---------------- 回调 ----------------
    def _on_image(self, msg):
        # 跑在 ros-spin 线程上。**不要在这里做任何耗时的活**：这个线程还
        # 负责 /camera/source、/detect/persons，卡住它会让切源的回报也变慢。
        with self._lock:
            if not self._running:
                return
            out = self.appsrc
        if out is None:
            return

        frame, err = image_to_bgr(msg.data, msg.width, msg.height, msg.step,
                                 msg.encoding)
        if frame is None:
            self._error(err)
            return
        if (frame.shape[1], frame.shape[0]) != (self.width, self.height):
            # 每一帧都要计数：ros_errors 是"这条流到底通不通"的证据，只按
            # 提醒频率累加的话，尺寸一直不对时它也会停在 3 不动，看上去像
            # "偶发错误"。刷屏由 _error 里的日志节流负责，和计数无关。
            self._error(
                "画面 %dx%d 和 pipeline 的 %dx%d 不一致 —— 逐帧被丢掉。"
                "让 camera_node 的 width/height（或 launch 参数）和 "
                "device_agent 的 --width/--height 对齐"
                % (frame.shape[1], frame.shape[0], self.width, self.height))
            return

        Gst = require_gst()
        try:
            buf = Gst.Buffer.new_wrapped(frame.tobytes())
        except Exception as exc:             # noqa: BLE001
            self._error("建 GstBuffer 失败: %s" % exc)
            return

        ret = out.push_buffer(buf)
        if ret == Gst.FlowReturn.OK:
            self.frames += 1
            self.last_at = time.monotonic()
        elif ret == Gst.FlowReturn.FLUSHING:
            pass                             # 正常收工（管线在往 NULL 走）
        else:
            self._error("推帧失败: %s" % ret)

    def _error(self, msg):
        self.errors += 1
        self.last_error = msg
        if self.errors <= 3 or self.errors % 100 == 0:
            self.log.warning("ROS 桥：%s（第 %d 次）", msg, self.errors)

    # ---------------- 统计 ----------------
    def counters(self):
        gap = (round((time.monotonic() - self.last_at) * 1000, 1)
               if self.last_at else None)
        return {
            "ros_topic": self.a.ros_image_topic,
            "ros_frames": self.frames,
            "ros_errors": self.errors,
            "ros_last_error": self.last_error,
            "ros_gap_ms": gap,
        }


# --------------------------------------------------------------------------- #
# 骨架 + 切换
# --------------------------------------------------------------------------- #
def skeleton_desc(a):
    """常驻骨架：appsrc → videoconvert → 硬件编码 → RTP，可选音频分支。

    appsrc 的属性沿用 detect 模式实测定下来的那一组（见 detect_mode.py 的
    skeleton_desc）：block=false + leaky-type=downstream 时队列恒为 2，
    push 永远返回 OK，慢下来只丢帧、不涨延迟、也不把 ros-spin 线程卡在
    push_buffer 里（卡住它的话，切源回报和检测统计会一起停）。

    和 detect 模式的区别只有两处：
      · 帧的来源是 ROS 话题，不是进程内的 appsink；
      · **没有 textoverlay**。ROS 模式的画面由 detector_node 负责画框，
        遥操作命令台不启用（没有 DataChannel），骨架里不需要叠加层。
    """
    enc = P.encoder_desc(a)
    desc = (
        "webrtcbin name=wbr bundle-policy=max-bundle "
        # do-timestamp=true：帧来自话题，没有 v4l2 buffer 的 pts 可抄。
        # 让 appsrc 按到达时刻打点，正是"直播源"该有的时间戳。
        "appsrc name=rossrc is-live=true format=time do-timestamp=true "
        "block=false leaky-type=downstream max-buffers=2 max-bytes=0 "
        # caps 必须写死：appsrc 默认 ANY，下游拿着 ANY 定不下来，整条
        # pipeline 的状态切换会 not-negotiated。
        "caps=video/x-raw,format=BGR,width={w},height={h},framerate={fps}/1 ! "
        # mpph264enc 不吃 BGR，必须转成 NV12；n-threads=2 理由同采集段。
        "videoconvert n-threads=2 ! "
        "{enc} ! h264parse name=vtail ! "
        "{tail}"
    ).format(w=a.width, h=a.height, fps=a.fps, enc=enc, tail=P.video_tail(a))
    if P.audio_enabled(a):
        # 音频分支和 monitor 模式共用：build_ros() 后面要按 sink_1 链接 `aq`。
        desc += " " + P.audio_branch(a)
    return desc


class RosSwitch:
    """骨架 + 本会话的图像订阅。**所有方法都只能在 GLib 线程调用。**

    对外接口和 pipeline.VideoSwitch 对齐（source / attach_first / switch /
    start_bridges / dispose / n_switches / overlay），session.py 不需要知道
    自己拿的是哪一种。

    和 VideoSwitch 最大的不同：**这里没有可替换的采集段**。摄像头在
    camera_node 里，换源是"发布一条请求 + 等它回报"，GStreamer 这一侧
    一个元素都不动，所以也不会影响正在协商/运行中的 PeerConnection。
    """

    def __init__(self, pipeline, appsrc, a, link, log_):
        self.pipeline = pipeline
        self.appsrc = appsrc
        self.a = a
        self.link = link
        self.log = log_
        self.bridge = None
        self.n_switches = 0
        self.overlay = None          # 没有 textoverlay；保留属性给 session
        self.current = None          # 本次会话记住的源（回报还没来时的回退）

    @property
    def source(self):
        """优先用 camera_node 的回报 —— 那是设备侧的事实。

        还没收到回报时退回本会话记住的那一路（页面/启动参数说想要的那个），
        两者都没有才返回 None，由 session 用自己的默认源。
        """
        if self.link is not None:
            reported = self.link.current_source()
            if reported:
                try:
                    return sources.resolve(reported)
                except KeyError as exc:
                    self.log.warning("%s —— 回报的源不认识，按本地选择显示",
                                     sources.message_of(exc))
        return self.current

    def attach_first(self, src):
        """记下初始源。**不碰任何 GStreamer 元素**（第一帧由话题来）。"""
        self.current = src
        self.log.info("ROS 视频源：%s（期望 %s，%s 由 camera_node 打开）",
                      src.name, ", ".join(sources.names()), src.device)
        self.log.info("  摄像头由 ROS 节点掌管：切源请走页面或 "
                      "ros2 topic pub %s std_msgs/String \"{data: usb}\"",
                      self.a.ros_select_topic)

    def switch(self, name):
        """请求换一路。返回最终（本地认为）生效的源名。

        **不等回报**：这里是 GLib 线程，camera_node 要拆旧管线、跑亮度闭环
        再开新管线，几秒钟。等它的是 HTTP 那一侧（device_agent.handle_camera
        用 link.wait_source），不堵 GStreamer 主循环。
        """
        src = sources.resolve(name)
        if self.link is None:
            raise RuntimeError("ROS 模式没有建好 rclpy 节点，无法发布切源请求")
        self.link.select(src.name)
        self.current = src
        self.n_switches += 1
        self.log.info("ROS 切源请求已发出：%s（等 camera_node 回报）", src.name)
        return src.name

    def start_bridges(self):
        """观看端接入、pipeline 进 PLAYING 之后才订阅图像 —— 理由见 RosBridge。"""
        if self.bridge is not None:
            return
        self.bridge = RosBridge(self.link, self.appsrc, self.a, self.log)
        self.bridge.start()

    def dispose(self):
        """拆整条管线：NULL → 等 NULL → 收订阅。

        这里**没有**「把采集段从管道上摘掉」那一步（detect/monitor 模式里
        那是设备 fd 回收的关键）：ROS 模式的管线里没有任何采集元素，
        摄像头属于 camera_node。
        """
        Gst = require_gst()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_state(2 * Gst.SECOND)
        P.confirm_null(self.pipeline, "整条 ROS pipeline", self.log)
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        self.appsrc = None

    def ros_counters(self):
        base = {
            "ros_topic": self.a.ros_image_topic,
            "ros_frames": 0,
            "ros_errors": 0,
            "ros_last_error": "",
            "ros_gap_ms": None,
            "ros_source": None,
            "ros_select_topic": self.a.ros_select_topic,
            "ros_persons": 0,
            "ros_persons_ms": 0.0,
            "ros_persons_age_ms": None,
        }
        src = self.source
        if src is not None:
            base["ros_source"] = src.name
        if self.link is not None:
            p = self.link.persons()
            base["ros_persons"] = p["count"]
            base["ros_persons_ms"] = p["inference_ms"]
            base["ros_persons_age_ms"] = p["age_ms"]
        if self.bridge is not None:
            base.update(self.bridge.counters())
            if base["ros_source"] is None:
                base["ros_source"] = self.current.name if self.current else None
        return base


# --------------------------------------------------------------------------- #
# 搭建
# --------------------------------------------------------------------------- #
def build_ros(a, ice, log_, source, on_audio_elements=None):
    """搭出 ROS 模式的整条管线，返回 (pipeline, webrtcbin, switch)。

    骨架里的 ICE、sink_0/sink_1 链接、音频回程全部复用 pipeline.py 的实现 ——
    所有模式在 WebRTC 这一层必须完全同源。
    """
    Gst = require_gst()

    # **必须在 parse_launch 之前把 GstApp import 进来。** rossrc 是 appsrc，
    # push_buffer 绑在 GstApp 命名空间上；PyGObject 按实例类型缓存包装对象，
    # import 晚了 `get_by_name("rossrc")` 拿到的是一个没有 push_buffer 的秃
    # GstAppSrc —— 每来一帧抛一次 AttributeError（被 spin 线程的 except 兜住），
    # 而 /stats 上只看到 ros_frames 恒为 0、ros_errors 也是 0，非常难查。
    # 板子上实测踩过。whitebalance 里这个函数就是"提前 import GstApp"。
    whitebalance.require_app_sink()

    link = getattr(a, "ros_link", None)
    if link is None:
        raise RuntimeError(
            "ROS 模式需要先建好 rclpy 节点（device_agent.main 里创建 RosLink "
            "并挂到 args.ros_link 上）；现在拿不到它")

    pipeline = Gst.parse_launch(skeleton_desc(a))
    webrtcbin = pipeline.get_by_name("wbr")
    if webrtcbin is None:
        raise RuntimeError("描述串里没找到 webrtcbin，parse_launch 可能失败了")
    appsrc = pipeline.get_by_name("rossrc")
    if appsrc is None:
        raise RuntimeError("描述串里没找到 rossrc（ROS 骨架写错了？）")

    P.apply_ice(webrtcbin, ice, log_)

    # sink 编号决定 m-line 顺序，和其它模式保持一致：视频 sink_0、音频 sink_1。
    if not P._link(pipeline, webrtcbin, "vq", "sink_0"):
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("视频分支无法连接到 webrtcbin 的 sink_0")

    if P.audio_enabled(a):
        if not P._link(pipeline, webrtcbin, "aq", "sink_1"):
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("音频分支无法连接到 webrtcbin 的 sink_1")
        P.attach_audio_return(webrtcbin, pipeline, a, log_, on_audio_elements)

    switch = RosSwitch(pipeline, appsrc, a, link, log_)
    try:
        switch.attach_first(source)
    except Exception:
        # 起不来时别把已经到 READY 的管线漏在这儿。
        pipeline.set_state(Gst.State.NULL)
        raise
    return pipeline, webrtcbin, switch
