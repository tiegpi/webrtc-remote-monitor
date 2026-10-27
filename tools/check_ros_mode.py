#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_ros_mode.py —— ROS 模式的单测（宿主机就能跑，不需要 rclpy/GStreamer/NPU）

    python3 tools/check_ros_mode.py

两边各测一半：

  · src/ros_mode.py        话题默认值、Image→BGR 的转换（含 step 填充和 rgb8）、
                           骨架描述串、RosSwitch 的切源与统计、RosBridge 的
                           推帧/尺寸检查/stop 之后不再推。Gst 用假的替身，
                           这样在 WSL 上也能跑。
  · ros2_ws/.../detector_core.py
                           letterbox、anchor 解码、person 过滤、NMS、
                           坐标映射、stride 反推、白平衡。
  · ros2_ws/.../camera_node.py、detector_node.py
                           静态扫一遍日志调用：rclpy 的 logger 不吃
                           %-占位符 + 位置参数，写错只在板子上启动时才炸。
  · src/device_agent.py    SIGTERM 处理器必须装在 rclpy.init 之后，否则 TERM
                           被降级成"关 context"，进程不退（板子上实测过）。

**不解码真实模型**：NPU 只有板子上有，这里验的是"喂进去的形状/坐标/
通道顺序对不对"，也就是换模型、改话题、动坐标映射时最容易悄悄错的那层。
整条链的真机验收在 tools/check_ros_mode.sh。
"""

import ast
import array
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ros2_ws" / "src" / "webrtc_detect"))

import device_agent    # noqa: E402
import pipeline        # noqa: E402
import ros_mode        # noqa: E402
import sources         # noqa: E402
import whitebalance    # noqa: E402
from webrtc_detect import detector_core as C  # noqa: E402

np = C.require_numpy()
cv2 = C.require_cv2()

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILS.append(name)
        print("FAIL  %-46s %s" % (name, exc))
    except Exception as exc:                     # noqa: BLE001
        FAILS.append(name)
        print("FAIL  %-46s 未预期的异常：%r" % (name, exc))
    else:
        print("PASS  %s" % name)


def eq(got, want, what=""):
    assert got == want, "%s期望 %r，实际 %r" % (what and what + "：" or "", want, got)


def close(got, want, tol=1e-3, what=""):
    assert abs(got - want) <= tol, "%s期望 %r±%r，实际 %r" % (
        what and what + "：" or "", want, tol, got)


# --------------------------------------------------------------------------- #
# 假的 Gst / 假的话题订阅
# --------------------------------------------------------------------------- #
class FakeBuffer:
    def __init__(self, data):
        self.data = data


class FakeGst:
    """只实现 ros_mode 用到的那几个名字。"""

    class FlowReturn:
        OK = "ok"
        FLUSHING = "flushing"
        ERROR = "error"

    class Buffer:
        @staticmethod
        def new_wrapped(data):
            return FakeBuffer(data)


class FakeAppSrc:
    def __init__(self, ret=FakeGst.FlowReturn.OK):
        self.ret = ret
        self.pushed = []

    def push_buffer(self, buf):
        self.pushed.append(buf)
        return self.ret


class FakeSub:
    pass


class FakeLink:
    """RosLink 的替身：只保留 RosSwitch / RosBridge 用到的接口。"""

    def __init__(self, source=None, persons=None):
        self.source = source
        self.selected = []
        self.subs = []
        self.unsubscribed = []
        self.persons_doc = persons or {
            "count": 2, "inference_ms": 21.5, "messages": 7, "age_ms": 120.0}

    # RosSwitch 用
    def current_source(self):
        return self.source

    def select(self, name):
        self.selected.append(name)

    def persons(self):
        return self.persons_doc

    # RosBridge 用
    def subscribe_image(self, topic, callback):
        sub = FakeSub()
        self.subs.append((topic, callback, sub))
        return sub

    def unsubscribe_image(self, sub):
        self.unsubscribed.append(sub)


def image_msg(frame, encoding="bgr8", step=None):
    h, w = frame.shape[:2]
    return SimpleNamespace(
        data=frame.tobytes(),
        width=w, height=h,
        step=step if step is not None else w * 3,
        encoding=encoding,
    )


def make_args(**kw):
    base = dict(
        mode="ros", width=1280, height=720, fps=30, bitrate=2500000, gop=10,
        no_audio=True, mic_device=None, audio_bitrate=32000,
        ros_image_topic=ros_mode.IMAGE_TOPIC_DEFAULT,
        ros_persons_topic=ros_mode.PERSONS_TOPIC_DEFAULT,
        ros_select_topic=ros_mode.SELECT_TOPIC_DEFAULT,
        ros_source_topic=ros_mode.SOURCE_TOPIC_DEFAULT,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# ros_mode：话题默认值
# --------------------------------------------------------------------------- #
def case_topic_defaults():
    eq(ros_mode.IMAGE_TOPIC_DEFAULT, "/detect/image_annotated")
    eq(ros_mode.PERSONS_TOPIC_DEFAULT, "/detect/persons")
    eq(ros_mode.SELECT_TOPIC_DEFAULT, "/camera/select")
    eq(ros_mode.SOURCE_TOPIC_DEFAULT, "/camera/source")


def case_args_defaults_agree():
    """device_agent 的默认值和 ros_mode 的常量必须是同一个 —— 两边各写一份
    就会在改话题时漏掉一边，症状是"发布端在等一个没人发的话题"。"""
    a = device_agent.parse_args([])
    eq(a.mode, "monitor", "不传 --mode")
    eq(a.ros_image_topic, ros_mode.IMAGE_TOPIC_DEFAULT)
    eq(a.ros_persons_topic, ros_mode.PERSONS_TOPIC_DEFAULT)
    eq(a.ros_select_topic, ros_mode.SELECT_TOPIC_DEFAULT)
    eq(a.ros_source_topic, ros_mode.SOURCE_TOPIC_DEFAULT)
    eq(device_agent.parse_args(["--mode", "ros"]).mode, "ros")


# --------------------------------------------------------------------------- #
# ros_mode：sensor_msgs/Image → BGR
# --------------------------------------------------------------------------- #
def case_image_compact():
    frame = np.arange(4 * 3 * 3, dtype=np.uint8).reshape(4, 3, 3)
    got, err = ros_mode.image_to_bgr(frame.tobytes(), 3, 4, 9, "bgr8")
    eq(err, None)
    eq(got.shape, (4, 3, 3), "shape")
    assert np.array_equal(got, frame), "紧凑排布应该逐像素一致"
    assert got.flags["C_CONTIGUOUS"], "帧必须连续，否则不能直接交给 GStreamer"


def case_image_padded_step():
    """行尾填充：每行多 8 字节垃圾。按 width*3 去 reshape 会把画面切成斜的。"""
    h, w, pad = 3, 5, 8
    rows = np.zeros((h, w * 3 + pad), dtype=np.uint8)
    for y in range(h):
        rows[y, :w * 3] = np.arange(w * 3, dtype=np.uint8) + y * 10
        rows[y, w * 3:] = 0xEE                # 填充区，必须被丢掉
    got, err = ros_mode.image_to_bgr(rows.tobytes(), w, h, w * 3 + pad, "bgr8")
    eq(err, None)
    eq(got.shape, (h, w, 3), "shape")
    assert np.array_equal(got, rows[:, :w * 3].reshape(h, w, 3)), "填充列混进来了"


def case_image_rgb8():
    rgb = np.zeros((2, 2, 3), dtype=np.uint8)
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = 10, 20, 30      # R, G, B
    got, err = ros_mode.image_to_bgr(rgb.tobytes(), 2, 2, 6, "rgb8")
    eq(err, None)
    eq(tuple(got[0, 0]), (30, 20, 10), "通道应当翻成 BGR")
    assert got.flags["C_CONTIGUOUS"], "翻转后必须复制成连续内存"


def case_image_rejects():
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    _, err = ros_mode.image_to_bgr(frame.tobytes(), 2, 2, 6, "yuv422")
    assert err and "编码" in err, "未知编码应当拒绝"
    _, err = ros_mode.image_to_bgr(frame.tobytes(), 0, 2, 6, "bgr8")
    assert err and "尺寸非法" in err, "0 宽应当拒绝"
    _, err = ros_mode.image_to_bgr(frame.tobytes(), 2, 2, 4, "bgr8")
    assert err and "step" in err, "step 小于一行应当拒绝"
    _, err = ros_mode.image_to_bgr(frame.tobytes()[:-3], 2, 2, 6, "bgr8")
    assert err and "不够" in err, "数据不足应当拒绝"


# --------------------------------------------------------------------------- #
# ros_mode：骨架描述串
# --------------------------------------------------------------------------- #
def case_skeleton():
    a = make_args(width=640, height=480, fps=25)
    d = ros_mode.skeleton_desc(a)
    assert "appsrc name=rossrc" in d
    assert "caps=video/x-raw,format=BGR,width=640,height=480,framerate=25/1" in d
    assert d.count("videoconvert") == 1, "骨架里只该有转换 BGR→NV12 的这一处"
    assert "mpph264enc" in d and "rtph264pay" in d
    assert "name=wbr" in d and "name=vq" in d
    # ROS 模式没有 DataChannel，遥操作叠加层不启用
    assert "textoverlay" not in d, "ROS 模式的框由 detector_node 画，骨架里不该有叠加层"
    # 帧来自话题，没有 v4l2 的 pts 可抄
    assert "do-timestamp=true" in d
    assert "leaky-type=downstream" in d and "max-buffers=2" in d
    # 切源只发一条话题，骨架里不能有采集元素
    for bad in ("v4l2src", "v4l2src0", "mppjpegdec"):
        assert bad not in d, "骨架里不该出现采集元素 %s" % bad


def case_skeleton_audio():
    quiet = ros_mode.skeleton_desc(make_args(no_audio=True, mic_device="hw:0"))
    loud = ros_mode.skeleton_desc(
        make_args(no_audio=False, mic_device="hw:1,0", audio_bitrate=48000))
    assert "alsasrc" not in quiet, "--no-audio 时不该有音频分支"
    assert "alsasrc device=hw:1,0" in loud
    assert "opusenc bitrate=48000" in loud
    assert "name=aq" in loud, "build_ros 要靠 aq 连 sink_1"
    # 音频分支必须和 monitor 模式逐字相同 —— 所有模式在 WebRTC 这一层同源
    assert pipeline.audio_branch(make_args(
        no_audio=False, mic_device="hw:1,0", audio_bitrate=48000)) in loud


# --------------------------------------------------------------------------- #
# ros_mode：RosSwitch
# --------------------------------------------------------------------------- #
def case_build_ros_imports_gstapp_first():
    """GstApp 必须在 parse_launch 之前 import。

    晚一步的话 `get_by_name("rossrc")` 拿到的是没有 push_buffer 的秃 GstAppSrc，
    每来一帧抛一次 AttributeError、被 spin 线程 except 兜住 —— /stats 上只看到
    ros_frames 恒为 0、ros_errors 也是 0。板子上实测踩过。
    """
    order = []

    class Boom(Exception):
        pass

    class FakeGst:
        @staticmethod
        def parse_launch(desc):
            order.append("parse")
            raise Boom()                 # 到此为止，后面不用真建管线

    old_gst, old_app = ros_mode.require_gst, whitebalance.require_app_sink
    ros_mode.require_gst = lambda: FakeGst
    whitebalance.require_app_sink = lambda: order.append("gstapp")
    try:
        a = make_args()
        a.ros_link = SimpleNamespace()    # build_ros 见到非 None 就往下走
        try:
            ros_mode.build_ros(a, {}, ros_mode.log, sources.resolve("usb"))
        except Boom:
            pass
        except Exception as exc:          # noqa: BLE001
            raise AssertionError("没走到 parse_launch：%r" % (exc,))
        else:
            raise AssertionError("FakeGst 应当抛 Boom")
    finally:
        ros_mode.require_gst, whitebalance.require_app_sink = old_gst, old_app

    eq(order[:2], ["gstapp", "parse"], "调用顺序")


def case_switch_attach_and_select():
    a = make_args()
    link = FakeLink(source="mipi")
    sw = ros_mode.RosSwitch(pipeline=None, appsrc=object(), a=a, link=link,
                            log_=ros_mode.log)
    sw.attach_first(sources.resolve("mipi"))
    eq(sw.n_switches, 0, "attach_first 不算切换")
    eq(sw.bridge, None, "没观看端时不该订阅图像")
    eq(sw.overlay, None, "ROS 模式没有叠加层")

    name = sw.switch("usb")
    eq(name, "usb")
    eq(link.selected, ["usb"], "切源应当只发一条 /camera/select")
    eq(sw.n_switches, 1)
    eq(sw.current.name, "usb")

    try:
        sw.switch("vga")
    except KeyError as exc:
        assert "未知摄像头" in str(exc), "不认识的源名必须报错"
    else:
        raise AssertionError("不认识的源名应当抛 KeyError")


def case_switch_source_prefers_report():
    """画面到底是哪一路，以 camera_node 的回报为准，不是本地的期望。"""
    sw = ros_mode.RosSwitch(pipeline=None, appsrc=object(),
                            a=make_args(), link=FakeLink(source="usb"),
                            log_=ros_mode.log)
    sw.attach_first(sources.resolve("mipi"))
    eq(sw.source.name, "usb", "回报优先于本地期望")

    sw.link.source = "vga"                     # 认不出来的回报
    eq(sw.source.name, "mipi", "认不出来时退回本地期望")

    sw.link.source = None                      # 还没收到回报
    eq(sw.source.name, "mipi")


def case_switch_start_bridges_once():
    a = make_args()
    link = FakeLink(source="mipi")
    appsrc = FakeAppSrc()
    sw = ros_mode.RosSwitch(pipeline=None, appsrc=appsrc, a=a, link=link,
                            log_=ros_mode.log)
    sw.start_bridges()
    sw.start_bridges()
    eq(len(link.subs), 1, "start_bridges 重复调用只该建一个订阅")
    topic, _cb, sub = link.subs[0]
    eq(topic, a.ros_image_topic)
    assert sw.bridge.appsrc is not None, "订阅建好了，桥就该拿着 appsrc"

    sw.bridge.stop()
    eq(link.unsubscribed, [sub], "停止桥要摘掉订阅")
    eq(sw.bridge.appsrc, None, "stop 后不能再留着 appsrc")


def case_switch_counters():
    a = make_args()
    link = FakeLink(source="usb")
    sw = ros_mode.RosSwitch(pipeline=None, appsrc=FakeAppSrc(), a=a, link=link,
                            log_=ros_mode.log)
    sw.attach_first(sources.resolve("mipi"))
    c = sw.ros_counters()                      # 还没有桥
    eq(c["ros_frames"], 0)
    eq(c["ros_source"], "usb", "没有会话时也要能从回报里知道当前源")
    eq(c["ros_persons"], 2)
    close(c["ros_persons_ms"], 21.5)
    eq(c["ros_topic"], a.ros_image_topic)
    eq(c["ros_select_topic"], a.ros_select_topic)

    sw.start_bridges()
    c = sw.ros_counters()
    assert "ros_frames" in c and "ros_errors" in c and "ros_gap_ms" in c


# --------------------------------------------------------------------------- #
# ros_mode：RosBridge
# --------------------------------------------------------------------------- #
def with_fake_gst(fn):
    """_on_image 里会调 require_gst()；宿主机上换成假的。"""
    saved = ros_mode._Gst
    ros_mode._Gst = FakeGst
    try:
        return fn()
    finally:
        ros_mode._Gst = saved


def make_bridge(frame_w=64, frame_h=48, ret=FakeGst.FlowReturn.OK):
    a = make_args(width=frame_w, height=frame_h)
    link = FakeLink()
    appsrc = FakeAppSrc(ret=ret)
    bridge = ros_mode.RosBridge(link, appsrc, a, ros_mode.log)
    bridge.start()
    _topic, cb, _sub = link.subs[-1]
    return bridge, link, appsrc, cb


def case_bridge_pushes():
    def run():
        bridge, _link, appsrc, cb = make_bridge()
        frame = np.full((48, 64, 3), 7, dtype=np.uint8)
        cb(image_msg(frame))
        eq(bridge.frames, 1)
        eq(bridge.errors, 0, bridge.last_error)
        eq(len(appsrc.pushed), 1)
        eq(len(appsrc.pushed[0].data), 48 * 64 * 3, "推给 appsrc 的字节数")
        assert appsrc.pushed[0].data == frame.tobytes(), "推上去的不是这一帧"
    with_fake_gst(run)


def case_bridge_size_mismatch():
    """尺寸不符必须**丢掉**并说清原因：appsrc 的 caps 写死，
    尺寸对不上后面每一帧都会被 GStreamer 拒绝，表现是黑屏。"""
    def run():
        bridge, _link, appsrc, cb = make_bridge()
        small = np.zeros((24, 32, 3), dtype=np.uint8)
        for _ in range(5):
            cb(image_msg(small))
        eq(bridge.frames, 0, "尺寸不符不该推帧")
        eq(len(appsrc.pushed), 0)
        eq(bridge.errors, 5, "每一帧都要计入 ros_errors")
        assert "不一致" in bridge.last_error, bridge.last_error
    with_fake_gst(run)


def case_bridge_bad_encoding():
    def run():
        bridge, _link, appsrc, cb = make_bridge()
        msg = image_msg(np.zeros((48, 64, 3), dtype=np.uint8), encoding="mono8")
        cb(msg)
        eq(bridge.frames, 0)
        eq(len(appsrc.pushed), 0)
        assert "编码" in bridge.last_error, bridge.last_error
    with_fake_gst(run)


def case_bridge_flushing_is_not_an_error():
    def run():
        bridge, _link, _appsrc, cb = make_bridge(ret=FakeGst.FlowReturn.FLUSHING)
        cb(image_msg(np.zeros((48, 64, 3), dtype=np.uint8)))
        eq(bridge.frames, 0, "FLUSHING 是管线收工，不算推成功")
        eq(bridge.errors, 0, "FLUSHING 不该记成错误")
    with_fake_gst(run)


def case_bridge_stop_stops_pushing():
    def run():
        bridge, _link, appsrc, cb = make_bridge()
        bridge.stop()
        cb(image_msg(np.zeros((48, 64, 3), dtype=np.uint8)))
        eq(len(appsrc.pushed), 0, "stop 之后回调必须立刻返回")
        eq(bridge.frames, 0)
    with_fake_gst(run)


def case_bridge_counters():
    def run():
        bridge, _link, _appsrc, cb = make_bridge()
        cb(image_msg(np.zeros((48, 64, 3), dtype=np.uint8)))
        c = bridge.counters()
        eq(c["ros_topic"], bridge.a.ros_image_topic)
        eq(c["ros_frames"], 1)
        eq(c["ros_errors"], 0)
        eq(c["ros_last_error"], "")
        assert c["ros_gap_ms"] is not None and c["ros_gap_ms"] >= 0
    with_fake_gst(run)


# --------------------------------------------------------------------------- #
# detector_core：预处理 / 后处理
# --------------------------------------------------------------------------- #
def case_letterbox():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    # 给中心一小块纯色（而不是单像素）：单像素经过 CV_INTER_LINEAR 会被
    # 摊到 2x2 上，中心点的值就不再是原值了。
    frame[352:368, 632:648] = (1, 2, 3)
    canvas, scale, pad_x, pad_y = C.letterbox_bgr(frame)
    eq(canvas.shape, (640, 640, 3))
    close(scale, 0.5)
    eq((pad_x, pad_y), (0, 140), "1280x720 缩到 640x360，上下各补 140")
    eq(tuple(canvas[0, 0]), (C.LETTERBOX_PAD,) * 3, "灰边值必须是 114")
    eq(tuple(canvas[320, 320]), (1, 2, 3), "中心块要落到缩放后的位置")
    # 竖图：左右补边
    canvas, scale, pad_x, pad_y = C.letterbox_bgr(
        np.zeros((1000, 500, 3), dtype=np.uint8))
    close(scale, 0.64)
    eq((pad_x, pad_y), (160, 0))

    try:
        C.letterbox_bgr(np.zeros((10, 10), dtype=np.uint8))
    except ValueError as exc:
        assert "HxWx3" in str(exc)
    else:
        raise AssertionError("非三通道应当抛 ValueError")


def _outputs(h=80, w=80, values=None):
    """3 尺度输出。values: {(k, y, x, field): 值}，field 见 decode() 的注释。"""
    outs = [np.zeros((3 * (5 + C.NUM_CLASSES), s, s), dtype=np.float32)
            for s in (h, h // 2, h // 4)]
    for (k, y, x, f), v in (values or {}).items():
        outs[0].reshape(3, 5 + C.NUM_CLASSES, h, w)[k, f, y, x] = v
    return outs


def case_decode_person():
    # anchor 0 / stride 8 / 格子 (10, 10)：
    #   cx = (0.5*2 - 0.5 + 10) * 8 = 84
    #   bw = (0.5*2)^2 * 10 = 10，bh = 13
    outs = _outputs(values={
        (0, 10, 10, 0): 0.5, (0, 10, 10, 1): 0.5,
        (0, 10, 10, 2): 0.5, (0, 10, 10, 3): 0.5,
        (0, 10, 10, 4): 0.9,               # obj
        (0, 10, 10, 5): 0.9,               # person 类
    })
    dets = C.decode(outs)
    eq(len(dets), 1, "只该有一个人框")
    score, cls, x, y, w, h = dets[0]
    close(score, 0.81, 1e-5, "score = obj × cls")
    eq(cls, C.PERSON_CLASS)
    close(x, 79.0, what="x1")
    close(y, 77.5, what="y1")
    close(w, 10.0, what="宽度")
    close(h, 13.0, what="高度")


def case_decode_drops_other_classes():
    outs = _outputs(values={
        (0, 10, 10, 2): 0.5, (0, 10, 10, 3): 0.5,
        (0, 10, 10, 4): 0.9,
        (0, 10, 10, 5 + 2): 0.9,           # bicycle，不是 person
    })
    eq(C.decode(outs), [], "只关心 person，别的类必须滤掉")


def case_decode_threshold():
    outs = _outputs(values={
        (0, 10, 10, 2): 0.5, (0, 10, 10, 3): 0.5,
        (0, 10, 10, 4): 0.4, (0, 10, 10, 5): 0.4,     # 0.16 < conf
    })
    eq(C.decode(outs), [], "低于 conf 的必须滤掉")


def case_decode_bad_layout():
    try:
        C.decode([np.zeros((10, 10, 10), dtype=np.float32)])
    except ValueError as exc:
        assert "布局" in str(exc)
    else:
        raise AssertionError("未知输出布局应当抛 ValueError")


def case_nms():
    dets = [
        (0.9, 0, 0, 0, 100, 100),
        (0.8, 0, 5, 5, 100, 100),        # 和第一个大幅重叠
        (0.7, 0, 500, 500, 100, 100),    # 另一个位置
    ]
    kept = C.nms(dets, iou=0.45)
    eq(len(kept), 2, "重叠的那个应当被压掉")
    eq([round(k[0], 1) for k in kept], [0.9, 0.7], "按 score 降序")


def case_to_frame_boxes():
    # 1280x720 进 640x640：scale=0.5、上下各补 140
    dets = [(0.9, 0, 100.0, 300.0, 50.0, 100.0)]
    boxes = C.to_frame_boxes(dets, 0.5, 0, 140, 1280, 720)
    eq(len(boxes), 1)
    eq(boxes[0][:4], (200, 320, 300, 520), "映射回原帧像素")
    close(boxes[0][4], 0.9)

    # 越界的框裁到画面内
    boxes = C.to_frame_boxes([(0.9, 0, -20.0, 300.0, 100.0, 100.0)],
                             0.5, 0, 140, 1280, 720)
    eq(boxes[0][0], 0, "x1 应当裁到 0")

    # 完全在灰边里的框直接丢掉，不要变成 0×0 的框
    boxes = C.to_frame_boxes([(0.9, 0, 100.0, 10.0, 20.0, 20.0)],
                             0.5, 0, 140, 1280, 720)
    eq(boxes, [], "归一化后没有面积时必须丢掉")


def case_frame_stride():
    eq(C.frame_stride(1280, 720, 720 * 3840), 3840, "紧凑 BGR")
    eq(C.frame_stride(1280, 720, 720 * 4096), 4096, "行尾填充能反推出来")
    eq(C.frame_stride(1280, 720, 1000), 3840, "除不尽按紧凑算")
    eq(C.frame_stride(1280, 720, 720 * 1024), 3840, "反推值比紧凑还小，按紧凑算")


def case_segment_decoder():
    """USB 默认走软件解码 —— 板子的 mppjpegdec 慢到 1.5 秒/帧还不报错。"""
    a = SimpleNamespace(width=1280, height=720, fps=30)
    usb = sources.resolve("usb")
    mipi = sources.resolve("mipi")

    eq(C.MJPEG_DECODER_DEFAULT, "sw", "默认解码器")
    d = C.segment_desc(a, usb)
    assert "image/jpeg,width=1280,height=720" in d, d
    assert " jpegdec !" in d, d
    assert "mppjpegdec" not in d, d
    assert d.endswith("appsink name=capsink max-buffers=2 drop=true "
                      "sync=false async=false"), d
    assert "video/x-raw,format=BGR" in d, d

    mpp = C.segment_desc(a, usb, "mpp")
    assert "mppjpegdec !" in mpp, mpp
    assert "jpegdec" not in mpp.replace("mppjpegdec", ""), mpp

    # raw 源（MIPI）没有解码这一步：UYVY 直接 videoconvert
    raw = C.segment_desc(a, mipi)
    assert "jpegdec" not in raw, raw
    assert "video/x-raw,format=UYVY,width=1280,height=720" in raw, raw

    try:
        C.segment_desc(a, usb, "vaapi")
    except ValueError as exc:
        assert "vaapi" in str(exc), exc
    else:
        raise AssertionError("不认识的解码器应当抛 ValueError")


def case_uint8_array():
    """发布前转成 array('B')；直接赋 bytes 会走逐元素转换（720p 439 ms/帧）。"""
    frame = np.arange(24, dtype=np.uint8).reshape(2, 4, 3)
    arr = C.as_uint8_array(frame)
    assert isinstance(arr, array.array), type(arr)
    eq(arr.typecode, "B", "元素类型")
    eq(bytes(arr), frame.tobytes(), "字节内容")
    # 非连续视图（切片）也要能吃下 —— frombytes 要求 C 连续
    view = frame[:, 1:3]
    assert not view.flags["C_CONTIGUOUS"]
    eq(bytes(C.as_uint8_array(view)), view.tobytes(), "切片视图")
    # 空帧不能炸
    eq(len(C.as_uint8_array(np.zeros((0, 0, 3), dtype=np.uint8))), 0)


def case_draw_boxes():
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    C.draw_boxes(frame, [(50, 50, 150, 200, 0.87)])
    assert frame[50, 100].tolist() != [0, 0, 0], "框线应当画在画面上"
    assert frame.sum() > 0
    # 贴着上边界的框：标签要翻到框内，不能画到画面外
    frame2 = np.zeros((240, 320, 3), dtype=np.uint8)
    C.draw_boxes(frame2, [(10, 2, 100, 100, 0.5)])
    assert frame2[:20, 10:60].sum() > 0, "贴边的标签没画出来"


# --------------------------------------------------------------------------- #
# detector_core：白平衡
# --------------------------------------------------------------------------- #
def case_color_balance_manual():
    cb = C.ColorBalance("1.0,1.0")
    eq(cb.auto, False)
    eq(cb.desc, "R×1.00 B×1.00")
    frame = np.full((8, 8, 3), 60, dtype=np.uint8)
    out = cb.apply(frame)
    assert np.array_equal(out, frame), "增益 1.0 时画面不该变"

    cb = C.ColorBalance("2.0,1.0")            # R 加倍
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    frame[..., 2] = 100                       # BGR 的 R
    out = cb.apply(frame)
    eq(int(out[0, 0, 2]), 200, "R 通道应当乘 2")
    eq(int(out[0, 0, 1]), 0, "G 不动")


def case_color_balance_auto():
    """灰世界：R/G/B 均值 100/50/25 → R×0.5、B×2.0，之后冻结。"""
    cb = C.ColorBalance("auto")
    eq(cb.desc, "自校准中")
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = 25, 50, 100

    for _ in range(whitebalance.WB_CALIB_FRAMES - 1):
        out = cb.apply(frame)
        assert np.array_equal(out, frame), "校准期间不该改画面"
    eq(cb.desc, "自校准中", "还没到帧数就不该冻结")

    cb.apply(frame)                           # 第 WB_CALIB_FRAMES 帧
    close(cb.kr, 0.5, 1e-6, "kr = G/R")
    close(cb.kb, 2.0, 1e-6, "kb = G/B")
    eq(cb.desc, "R×0.50 B×2.00", "校准完 desc 必须说出实际增益")

    out = cb.apply(frame)                     # 冻结之后才真的校正
    eq(int(out[0, 0, 2]), 50, "R 100 × 0.5")
    eq(int(out[0, 0, 1]), 50, "G 不校正，还是 50")
    eq(int(out[0, 0, 0]), 50, "B 25 × 2.0")


def case_color_balance_too_dark():
    """太暗的帧估不准增益，必须退回默认值 —— 而不是算出一个离谱的倍数。"""
    cb = C.ColorBalance("auto")
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = 1, 3, 2
    for _ in range(whitebalance.WB_CALIB_FRAMES):
        cb.apply(frame)
    eq((round(cb.kr, 2), round(cb.kb, 2)),
       tuple(round(g, 2) for g in whitebalance.WB_DEFAULT_GAINS),
       "太暗时应当退回默认增益")
    assert "R×" in cb.desc


# --------------------------------------------------------------------------- #
# 节点源码：日志调用的写法
#
# rclpy 的 RcutilsLogger **不是** logging.Logger：它只收一个格式化好的字符串
# （或者关键字占位符），多给一个位置参数就是 TypeError。这个错只在节点启动、
# 切源、出错这些分支上触发 —— 上面的单测一条都跑不到，板子上真的这样崩过：
#
#   TypeError: RcutilsLogger.info() takes 2 positional arguments but 9 were given
#
# 所以在这里静态扫一遍。改日志文案时如果又写成 `info(fmt, a, b)`，宿主机上
# 就会红，不用等部署到板子。
NODE_DIRS = ROOT / "ros2_ws" / "src" / "webrtc_detect" / "webrtc_detect"


def case_logger_calls():
    bad = []
    for path in sorted(NODE_DIRS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # 形如 <x>.get_logger().info(...)
            if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Call)):
                continue
            inner = fn.value.func
            if not (isinstance(inner, ast.Attribute) and inner.attr == "get_logger"):
                continue
            if len(node.args) > 1:
                bad.append("%s:%d %s() 传了 %d 个位置参数"
                           % (path.name, node.lineno, fn.attr, len(node.args)))
    assert not bad, "要先 %-格式化成一个字符串：" + "；".join(bad)


# --------------------------------------------------------------------------- #
def case_sigterm_handler():
    """SIGTERM 要被换成 KeyboardInterrupt，而且必须装在 rclpy.init 之后。

    rclpy.init() 把 SIGTERM 的默认动作（终止进程）换成"关掉 context"，进程自己
    不退 —— 板子上实测 TERM 之后 20 秒还在，service.sh 只能干等 10 秒再 SIGKILL。
    装晚了则被 rclpy 覆盖回同样的行为。
    """
    path = ROOT / "src" / "device_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))

    def is_call(node, owner, attr):
        if not isinstance(node, ast.Call):
            return False
        f = node.func
        return (isinstance(f, ast.Attribute) and f.attr == attr
                and isinstance(f.value, ast.Name) and f.value.id == owner)

    term = init = None
    for node in ast.walk(tree):
        if is_call(node, "signal", "signal"):
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Attribute) and arg.attr == "SIGTERM":
                term = node.lineno
        elif is_call(node, "rclpy", "init"):
            init = node.lineno
    assert term is not None, "device_agent 里没有 signal.signal(signal.SIGTERM, ...)"
    assert init is not None, "device_agent 里没有 rclpy.init()"
    assert term > init, ("signal.signal(SIGTERM) 在 rclpy.init 之前（%d < %d），"
                         "会被 rclpy 的处理器覆盖掉" % (term, init))

    # 处理器本身：真的抛 KeyboardInterrupt，才能走 main() 的收尾路径。
    handler = getattr(device_agent, "_raise_keyboard_interrupt", None)
    assert handler is not None, "device_agent 里没有 _raise_keyboard_interrupt"
    try:
        handler(None, None)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("_raise_keyboard_interrupt 没有抛 KeyboardInterrupt")


# --------------------------------------------------------------------------- #
def main():
    cases = [
        # ros_mode
        ("话题默认值", case_topic_defaults),
        ("device_agent 的 ROS 参数默认值一致", case_args_defaults_agree),
        ("device_agent：TERM 走 KeyboardInterrupt 收尾", case_sigterm_handler),
        ("image_to_bgr：紧凑 BGR", case_image_compact),
        ("image_to_bgr：step 有行尾填充", case_image_padded_step),
        ("image_to_bgr：rgb8 换通道", case_image_rgb8),
        ("image_to_bgr：非法输入给出原因", case_image_rejects),
        ("骨架：caps/编码器/无叠加层", case_skeleton),
        ("骨架：音频分支与 monitor 同源", case_skeleton_audio),
        ("build_ros：GstApp 先于 parse_launch", case_build_ros_imports_gstapp_first),
        ("RosSwitch：attach_first 与切源", case_switch_attach_and_select),
        ("RosSwitch：源以 camera_node 回报为准", case_switch_source_prefers_report),
        ("RosSwitch：start_bridges 只建一个订阅", case_switch_start_bridges_once),
        ("RosSwitch：ros_counters 合成两边数字", case_switch_counters),
        ("RosBridge：按帧推给 appsrc", case_bridge_pushes),
        ("RosBridge：尺寸不符丢帧并报原因", case_bridge_size_mismatch),
        ("RosBridge：不认识的编码丢帧", case_bridge_bad_encoding),
        ("RosBridge：FLUSHING 不算错误", case_bridge_flushing_is_not_an_error),
        ("RosBridge：stop 之后不再推", case_bridge_stop_stops_pushing),
        ("RosBridge：counters 字段", case_bridge_counters),
        # detector_core
        ("letterbox：等比缩放 + 灰边", case_letterbox),
        ("decode：anchor 解码出 person", case_decode_person),
        ("decode：只留 person", case_decode_drops_other_classes),
        ("decode：低于 conf 不要", case_decode_threshold),
        ("decode：布局不认识就报错", case_decode_bad_layout),
        ("nms：重叠框只留一个", case_nms),
        ("to_frame_boxes：映射回原帧并裁剪", case_to_frame_boxes),
        ("frame_stride：行尾填充反推", case_frame_stride),
        ("采集段：USB 默认软件解码、MIPI 不过解码器", case_segment_decoder),
        ("发布：帧转 array('B')", case_uint8_array),
        ("draw_boxes：框和贴边标签", case_draw_boxes),
        ("ColorBalance：手动增益", case_color_balance_manual),
        ("ColorBalance：灰世界自校准", case_color_balance_auto),
        ("ColorBalance：太暗退回默认", case_color_balance_too_dark),
        # 节点源码
        ("节点日志：只传一个格式化好的字符串", case_logger_calls),
    ]
    for name, fn in cases:
        check(name, fn)
    ok = len(cases) - len(FAILS)
    print("\n%d/%d 通过" % (ok, len(cases)))
    if FAILS:
        print("失败：" + "；".join(FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.CRITICAL)
    raise SystemExit(main())
