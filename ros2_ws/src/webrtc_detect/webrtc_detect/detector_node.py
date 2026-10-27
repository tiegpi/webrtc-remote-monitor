#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detector_node —— 订阅原图，推理，把框烧进画面再发出去

    /camera/image_raw ──┐
                        ├─ 推理线程（限频）──> /detect/persons（JSON）
                        │                         ▲
                        └─ 每帧画上"最近一次的框" ─┴─> /detect/image_annotated

为什么**每一帧都发**标注图，而不是只发推理过的那几帧：预览要的是流畅的画面，
推理是限频的（默认 5 fps，视频 30 fps）。两个频率分开，中间帧复用上一次的
框 —— 这和 detect 模式里"框烧进 H.264"的做法一致，只是把那条链拆成了两个
ROS 节点。代价是框最多晚 1/detect_fps 秒，对"看有没有人"完全够用。

为什么推理在**单独的线程**：RKNN 一次 20~30 ms，放在订阅回调里会把 executor
堵住，检测频率一高，话题上的其它回调（比如以后的参数/服务）跟着排队。
回调只做"取帧、画框、发图"，推理线程只做 NPU。

检测起不来（模型缺失/NPU 初始化失败）时**视频照常转发**：节点继续把原图当
标注图发出去，只是没有框。理由和 detect 模式一样 —— 检测坏了不该把预览
一起拖死，否则排查方向会被带偏到 WebRTC 上。

JSON 是 std_msgs/String，不是自定义消息：板子上没有外网、也没装 vision_msgs，
而 rosidl 自定义消息要单独编一个包。规划/控制节点用 json 解一下就能拿到框。
"""

import json
import logging
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from std_msgs.msg import String

from . import detector_core as C

log = logging.getLogger("detector_node")


class DetectorNode(Node):
    def __init__(self):
        super().__init__("detector_node")

        self.declare_parameter("image_topic", "/camera/image_raw")
        self.declare_parameter("annotated_topic", "/detect/image_annotated")
        self.declare_parameter("persons_topic", "/detect/persons")
        self.declare_parameter("model", C.MODEL_DEFAULT)
        self.declare_parameter("detect_fps", C.DETECT_FPS_DEFAULT)
        self.declare_parameter("conf", C.CONF_DEFAULT)
        self.declare_parameter("iou", C.IOU_DEFAULT)

        gp = self.get_parameter
        self.model = str(gp("model").value)
        self.detect_fps = max(0.1, float(gp("detect_fps").value))
        self.conf = float(gp("conf").value)
        self.iou = float(gp("iou").value)

        self._pub_annotated = self.create_publisher(
            Image, str(gp("annotated_topic").value), qos_profile_sensor_data)
        self._pub_persons = self.create_publisher(
            String, str(gp("persons_topic").value), 10)
        self._sub_image = self.create_subscription(
            Image, str(gp("image_topic").value), self._on_image,
            qos_profile_sensor_data)

        self._lock = threading.Lock()
        self._boxes = []
        self._pending = None              # 待推理的一帧（只留最新的一张）
        self._pending_at = 0.0
        self._pending_header = None
        self._infer_event = threading.Event()
        self._last_infer_req = None
        self._stop = threading.Event()

        # 读这些数的是 10 秒一条的状态日志和 /stats（经 device_agent）；写的是
        # 两个线程。普通 int/float，GIL 下足够。
        self.frames_in = 0
        self.frames_out = 0
        self.inferences = 0
        self.boxes_total = 0
        self.errors = 0
        self.last_ms = 0.0
        self.last_n = 0
        self.last_error = ""
        self.ready = False

        self._worker = threading.Thread(
            target=self._infer_loop, name="detector-infer", daemon=True)
        self._worker.start()
        self._timer = self.create_timer(10.0, self._report)

        # rclpy 的 logger **不支持** %-占位符 + 位置参数（不是 logging 的
        # Logger）：多传一个位置参数就是 TypeError。所以先自己 %-格式化好，
        # 只传一个字符串进去。本文件里所有 get_logger() 都按这个写法来。
        self.get_logger().info(
            "detector_node：%s → %s + %s（推理上限 %.1f fps，conf %.2f）"
            % (gp("image_topic").value, gp("annotated_topic").value,
               gp("persons_topic").value, self.detect_fps, self.conf))
        # 只是提前说一声；节点照常启动 —— 原样转发画面比"整个节点起不来"有用。
        problem = C.check_available(self.model)
        if problem:
            self.get_logger().warn("检测依赖有问题：%s" % problem)

    # ---------------- 订阅回调（rclpy spin 线程） ----------------
    def _on_image(self, msg):
        frame, err = C.image_to_bgr(msg.data, msg.width, msg.height,
                                    msg.step, msg.encoding)
        if frame is None:
            self._error("输入画面不可用：%s" % err)
            return
        self.frames_in += 1

        # 限频推理：只在到期且推理线程空闲时递一张过去。**不排队** ——
        # 排队只会让框越来越旧，实时画面要的是"最新的一张"。
        now = time.monotonic()
        due = (self._last_infer_req is None or
               (now - self._last_infer_req) >= 1.0 / self.detect_fps)
        if due and not self._infer_event.is_set():
            self._last_infer_req = now
            # 必须复制：回调返回后 msg 的 data 可能被 rclpy 回收。
            self._pending = frame.copy()
            self._pending_at = now
            self._pending_header = msg.header
            self._infer_event.set()

        with self._lock:
            boxes = list(self._boxes)
        self._publish_annotated(msg, frame, boxes)

    def _publish_annotated(self, msg, frame, boxes):
        out = Image()
        out.header = msg.header
        out.height = int(frame.shape[0])
        out.width = int(frame.shape[1])
        out.encoding = "bgr8"
        out.is_bigendian = False
        out.step = out.width * 3
        # 每一帧都要发，所以这里必须是快路径：bytes 赋给 uint8[] 字段是逐元素
        # 转的（720p 439 ms/帧），array('B') 是 0.3 ms。见 as_uint8_array。
        if boxes:
            try:
                canvas = frame.copy()
                C.draw_boxes(canvas, boxes)
                out.data = C.as_uint8_array(canvas)
            except Exception as exc:         # noqa: BLE001
                self._error("画框失败: %s" % exc)
                out.data = C.as_uint8_array(frame)
        else:
            # 没有框：原样转发。as_uint8_array 里的 ascontiguousarray 对已经
            # 连续的帧不会额外复制，后面那次 frombytes 才是必须的拷贝。
            out.data = C.as_uint8_array(frame)
        self._pub_annotated.publish(out)
        self.frames_out += 1

    # ---------------- 推理线程 ----------------
    def _infer_loop(self):
        runner = None
        try:
            try:
                runner = C.RknnRunner(self.model, conf=self.conf, iou=self.iou)
                self.ready = True
                self.get_logger().info("NPU 已就绪（模型 %s）" % self.model)
            except Exception as exc:         # noqa: BLE001
                self.ready = False
                self._error("NPU 不可用，视频照常转发但没有检测框：%s" % exc)

            while not self._stop.is_set():
                if not self._infer_event.wait(0.2):
                    continue
                self._infer_event.clear()
                if self._stop.is_set():
                    break
                frame = self._pending
                header = self._pending_header
                self._pending = None
                self._pending_header = None
                if frame is None or runner is None:
                    continue
                try:
                    boxes, ms = runner.infer(frame)
                except Exception as exc:     # noqa: BLE001
                    self._error("推理失败: %s" % exc)
                    # 保留上一次的框：短暂失败时画面不会突然闪空。
                    continue
                with self._lock:
                    self._boxes = boxes
                self.inferences += 1
                self.last_ms = ms
                self.last_n = len(boxes)
                self.boxes_total += self.last_n
                self._publish_persons(boxes, ms, header, frame.shape)
        finally:
            if runner is not None:
                # release 必须发生在**用过它的线程**上（别的线程 release
                # 会在 librknnrt 里段错误，detect_canvas 上踩过）。
                self.get_logger().info("释放 NPU 上下文（线程 %s）"
                                       % threading.current_thread().name)
                runner.release()

    def _publish_persons(self, boxes, ms, header, shape):
        payload = {
            "count": len(boxes),
            "inference_ms": round(float(ms), 1),
            "stamp": stamp_of(header),
            # 框坐标是**这一帧**的像素坐标，所以帧尺寸必须一起发：下游
            # （规划/控制）拿到的框要能直接换算到自己的坐标系。
            "frame": {"height": int(shape[0]), "width": int(shape[1])},
            "boxes": [
                {"label": "person", "score": round(float(score), 3),
                 "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2)}
                for x1, y1, x2, y2, score in boxes
            ],
        }
        self._pub_persons.publish(String(data=json.dumps(payload)))

    # ---------------- 收尾 / 状态 ----------------
    def _error(self, msg):
        self.errors += 1
        self.last_error = msg
        if self.errors <= 3 or self.errors % 100 == 0:
            self.get_logger().warn("%s（第 %d 次）" % (msg, self.errors))

    def _report(self):
        self.get_logger().info(
            "输入 %d 帧 / 输出 %d 帧 / 推理 %d 次 / 累计 %d 人 / "
            "最近 %.1f ms、%d 人 / 错误 %d"
            % (self.frames_in, self.frames_out, self.inferences,
               self.boxes_total, self.last_ms, self.last_n, self.errors))

    def shutdown(self):
        self._stop.set()
        self._infer_event.set()          # 叫醒可能正等在事件上的线程
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.join(5.0)
            if worker.is_alive():
                self.get_logger().warn("推理线程 5 秒内没停下")


def stamp_of(header):
    """header.stamp → 可读的 JSON（秒 + 纳秒）。

    没有 header（理论上不会有）就返回 None，而不是编一个时间出来 ——
    下游据此能分辨"这条没带时间"。
    """
    if header is None:
        return None
    return {"sec": int(header.stamp.sec), "nanosec": int(header.stamp.nanosec)}


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    rclpy.init(args=None)
    node = None
    try:
        node = DetectorNode()
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
