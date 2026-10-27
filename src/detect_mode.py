#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_mode.py —— 轻量化直连模式：采集 → OpenCV → RKNN → 画框 → WebRTC

和 monitor 模式（阶段一/二/三）相比，这条链**不经过任何机器人框架**，也不
复用 textoverlay 那套叠加层：摄像头出来的帧先被 Python 接住，做 OpenCV
预处理、RKNN 推理，把框**烧进画面**，再交给硬件编码器。

    采集段（可替换）                        骨架（建一次，永不动）
    v4l2src ! caps ! [mppjpegdec]            appsrc ! videoconvert ! mpph264enc
      ! videoconvert ! BGR ! appsink  ──┐      ! h264parse ! rtph264pay ! webrtcbin
                                        │
                              DetectBridge 线程
                       拉帧 → 白平衡(可选) → 推理(限频) → 画框 → push

为什么框要烧进推流画面，而不是像 detect_canvas 那样让页面用 canvas 叠：
  · 任何观看端都看得到 —— 浏览器、ffplay、录像文件、截图，不需要页面配合；
  · 页面只需要一个 <video>，没有"检测结果和画面不同步"的第二条时间线；
  · 代价是每帧都要过一遍 Python，所以这条模式定位就是**轻量、单路**。

为什么另起一个文件，而不是把分支塞进 pipeline.py：
  · detect 模式的采集段没有 textoverlay、没有白平衡桥的 UYVY 域约束，
    Python 线程同时负责"解码后的帧"和"编码前的帧"两头，生命周期和
    VideoSegment/WhiteBalanceBridge 都不一样；
  · 但骨架、ICE、音频回程、RTP 尾段全部复用 pipeline.py —— 两条模式在
    WebRTC 这一层必须是同一份代码，否则修一个 bug 要修两遍。

线程归属（和项目里其它跨线程代码同一个约定）：
  · Gst 元素（加/删段、sync 状态）只在 GLib 线程碰，见 DetectSwitch；
  · 拉 appsink、推理、推 appsrc 全在 DetectBridge 自己的线程里；
  · 计数器是普通 int/float，读的人在 1Hz 遥测线程，GIL 下不加锁。

依赖（numpy / cv2 / rknnlite）全部**惰性 import**：monitor 模式不 import
本模块的任何重依赖，detect 模式缺依赖时也要能给出人话，而不是 ImportError。
"""

import logging
import os
import threading
import time

import pipeline as P
import sources
import whitebalance

log = logging.getLogger("detect")

#: 板子上现成的 YOLOv5s。模型不入库（8 MB 权重 + AGPL），换模型用
#: --detect-model；输出布局必须仍是 3 尺度 × 3 anchor × (5+80 类)。
MODEL_DEFAULT = "/userdata/rknn_yolov5_demo/model/RK3588/yolov5s-640-640.rknn"

INPUT_SIZE = 640
PERSON_CLASS = 0                 # COCO 的 person
NUM_CLASSES = 80
LETTERBOX_PAD = 114              # YOLOv5 训练时的灰边值，别改成 0

# YOLOv5s 的三组 anchor（stride 8/16/32）。这三组数是实测过的：拿板子上的
# bus.jpg 解出来的人框和原图对得上；换 anchor 会整体偏大/偏小。
ANCHORS = (
    ((10, 13), (16, 30), (33, 23)),
    ((30, 61), (62, 45), (59, 119)),
    ((116, 90), (156, 198), (373, 326)),
)
STRIDES = (8, 16, 32)

CONF_DEFAULT = 0.35
IOU_DEFAULT = 0.45
DETECT_FPS_DEFAULT = 5.0         # 推理频率；视频帧率仍是 --fps，别混

#: 拉帧超时。够短，换源/拆会话时线程能及时退出，不会把 GLib 线程卡住。
POLL_NS = 100 * 1000 * 1000
#: 一直拉不到帧就自己收工，别把线程永远挂着。和 whitebalance 同款兜底。
IDLE_GIVEUP_NS = 5 * 1000 * 1000 * 1000

_np = None
_cv2 = None
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
                "缺少 numpy —— detect 模式要用它接帧/画框\n"
                "  板子:  sudo apt install python3-numpy\n"
                "  其它:  pip3 install numpy"
            ) from None
        _np = numpy
    return _np


def require_cv2():
    global _cv2
    if _cv2 is None:
        try:
            import cv2
        except Exception as exc:         # noqa: BLE001
            # 板子上最容易踩的其实不是"没装 OpenCV"，而是用户目录里的
            # numpy 2.x 盖住了系统给 python3-opencv 编的 1.x。那种情况下
            # import cv2 抛的是 numpy 的 ABI 报错，不是 ImportError。
            hint = ""
            if "numpy" in str(exc).lower():
                hint = ("\n  这是 numpy 版本冲突：用户目录里的 numpy 2.x 盖住了"
                        "系统 numpy 1.x（python3-opencv 是按 1.x 编的）。"
                        "\n  用 python3 -s 跑，或设置 PYTHONNOUSERSITE=1。")
            raise RuntimeError("OpenCV 不可用: %s%s" % (exc, hint)) from None
        _cv2 = cv2
    return _cv2


def require_gst():
    global _Gst
    if _Gst is None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        _Gst = Gst
    return _Gst


def check_available(model_path=MODEL_DEFAULT):
    """启动前检查依赖，返回错误串或 None（和 whitebalance 同款）。

    这里**不加载模型**：load/init 会把 NPU 上下文建起来，不该在没有观看端
    时就占着。真正加载在 DetectBridge 的线程里。
    """
    try:
        require_numpy()
    except RuntimeError as exc:
        return str(exc)
    try:
        require_cv2()
    except RuntimeError as exc:
        return str(exc)
    try:
        from rknnlite.api import RKNNLite  # noqa: F401
    except ImportError:
        return ("缺少 rknn-toolkit-lite2（板子厂商镜像通常自带；"
                "没有就按 Rockchip 的 rknn-toolkit2 装 Python 绑定）")
    if not os.path.isfile(model_path):
        return ("模型文件不存在: %s\n"
                "  用 --detect-model 指到实际的 .rknn；模型不入库，"
                "来源见 README 的「detect 模式」一节" % model_path)
    return None


# --------------------------------------------------------------------------- #
# 纯 numpy/cv2：预处理 / 后处理（宿主机可单测，不需要 GStreamer/NPU）
# --------------------------------------------------------------------------- #
def frame_stride(width, height, nbytes):
    """一行占多少字节（BGR）。

    videoconvert 出来的是紧凑 BGR，但行尾可能有对齐填充：stride 往往是
    「宽度*3 按 4 字节向上对齐」而不是宽度*3。一帧 buffer 的大小正好是
    stride*height，所以直接除就能反推；除不尽就按紧凑算 —— 宁可少一层
    保险，也不能因为推不出来就丢帧。

    **别改用 `GstVideo.VideoInfo.from_caps()`**（detect_canvas 上实测的坑）：
    只要它在这个检测线程里被调用过，收尾时 `rknn.release()` 就会在
    librknnrt 里段错误。本文件**完全不碰 GstVideo**，就是为了避开它。
    """
    compact = int(width) * 3
    height = int(height)
    nbytes = int(nbytes)
    if height > 0 and nbytes % height == 0:
        cand = nbytes // height
        if cand >= compact:
            return cand
    return compact


def letterbox_bgr(bgr, size=INPUT_SIZE, pad_value=LETTERBOX_PAD):
    """等比缩放并补边到 size×size，返回 (canvas, scale, pad_x, pad_y)。

    补灰边而不是拉伸：YOLOv5 训练时就是这么做的，硬拉会把人的比例弄坏。
    用 OpenCV 的线性插值 —— 这是 detect 模式的"OpenCV 预处理"那一半。
    """
    cv2 = require_cv2()
    np = require_numpy()
    bgr = np.asarray(bgr)
    if bgr.ndim != 3 or bgr.shape[2] != 3:
        raise ValueError("期望 HxWx3 的 BGR，实际 shape=%r" % (bgr.shape,))
    h, w = int(bgr.shape[0]), int(bgr.shape[1])
    if h <= 0 or w <= 0:
        raise ValueError("帧尺寸非法: %dx%d" % (w, h))

    scale = min(float(size) / w, float(size) / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)

    canvas = np.full((size, size, 3), pad_value, dtype=np.uint8)
    pad_x = (size - nw) // 2
    pad_y = (size - nh) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y


def _iou(a, b):
    """两个 [score, cls, x, y, w, h] 框的 IoU。"""
    ax, ay, aw, ah = a[2], a[3], a[4], a[5]
    bx, by, bw, bh = b[2], b[3], b[4], b[5]
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def nms(dets, iou=IOU_DEFAULT):
    """类别无关的 NMS。这里只留 person 一类，不需要按类分开做。"""
    ordered = sorted(dets, key=lambda d: d[0], reverse=True)
    keep = []
    for d in ordered:
        if all(_iou(d, k) <= iou for k in keep):
            keep.append(d)
    return keep


def decode(outputs, conf=CONF_DEFAULT, iou=IOU_DEFAULT):
    """YOLOv5 三尺度输出 → person 框列表。

    每个输出是 (1, 3*(5+80), h, w) 或 (3*(5+80), h, w)。模型已经被
    rknnlite 反量化成 float32，值在 0~1（sigmoid 已经做过），所以这里
    **不再**做 sigmoid/zero-point，只做 anchor 解码：

        cx = (x*2 - 0.5 + grid_x) * stride
        w  = (w*2)^2 * anchor_w

    框坐标是 640×640 输入空间的像素；调用方再用 letterbox 参数映射回原帧。
    返回 [score, 0, x, y, w, h]，已做 NMS，按 score 降序。
    """
    np = require_numpy()
    dets = []
    for out, anchors, stride in zip(outputs, ANCHORS, STRIDES):
        a = np.asarray(out, dtype=np.float32)
        if a.ndim == 4:
            a = a[0]
        expect = 3 * (5 + NUM_CLASSES)
        if a.ndim != 3 or a.shape[0] != expect:
            raise ValueError(
                "检测输出布局不认识：shape=%r，期望 (3*(5+80), h, w)。"
                "换模型时要么保持 YOLOv5 布局，要么改 decode()"
                % (a.shape,))
        h, w = int(a.shape[1]), int(a.shape[2])
        # (3, 85, h, w) -> (3, h, w, 85)；最后一维顺序是 x,y,w,h,obj,cls...
        p = a.reshape(3, 5 + NUM_CLASSES, h, w).transpose(0, 2, 3, 1)

        obj = p[..., 4]
        cls_p = p[..., 5:]
        score = obj * cls_p.max(axis=-1)
        cls = cls_p.argmax(axis=-1)
        mask = (score >= conf) & (cls == PERSON_CLASS)
        ks, ys, xs = np.nonzero(mask)
        if not len(ks):
            continue

        anc = np.asarray(anchors, dtype=np.float32)[ks]
        px = p[ks, ys, xs, 0]
        py = p[ks, ys, xs, 1]
        pw = p[ks, ys, xs, 2]
        ph = p[ks, ys, xs, 3]
        cx = (px * 2.0 - 0.5 + xs) * stride
        cy = (py * 2.0 - 0.5 + ys) * stride
        bw = ((pw * 2.0) ** 2) * anc[:, 0]
        bh = ((ph * 2.0) ** 2) * anc[:, 1]
        for i in range(len(ks)):
            dets.append((float(score[ks[i], ys[i], xs[i]]), PERSON_CLASS,
                         float(cx[i] - bw[i] / 2.0),
                         float(cy[i] - bh[i] / 2.0),
                         float(bw[i]), float(bh[i])))
    return nms(dets, iou)


def to_frame_boxes(dets, scale, pad_x, pad_y, frame_w, frame_h):
    """640×640 输入坐标 → 原帧像素坐标，裁到画面内。

    返回 [(x1, y1, x2, y2, score), ...]，整数像素。画框直接用这个，
    不需要像页面叠加层那样归一化 —— 烧进画面的人知道帧有多大。
    """
    out = []
    for score, _cls, x, y, w, h in dets:
        x1 = (x - pad_x) / scale
        y1 = (y - pad_y) / scale
        x2 = x1 + w / scale
        y2 = y1 + h / scale
        x1 = min(max(x1, 0.0), float(frame_w))
        y1 = min(max(y1, 0.0), float(frame_h))
        x2 = min(max(x2, 0.0), float(frame_w))
        y2 = min(max(y2, 0.0), float(frame_h))
        if x2 - x1 < 1.0 or y2 - y1 < 1.0:
            continue
        out.append((int(round(x1)), int(round(y1)),
                    int(round(x2)), int(round(y2)), float(score)))
    return out


def draw_boxes(bgr, boxes):
    """把框和置信度画在 BGR 帧上（原地修改）。

    颜色用 BGR 的绿色 (0,255,0)。标签只写 ASCII —— 板子上没有中文字体，
    写中文会变成一排方框（monitor 模式的 textoverlay 也是这个原因）。
    """
    cv2 = require_cv2()
    for x1, y1, x2, y2, score in boxes:
        cv2.rectangle(bgr, (x1, y1), (x2, y2), (0, 255, 0), 2, cv2.LINE_AA)
        label = "person %.2f" % score
        (tw, th), _base = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        ty = y1 - 6 if y1 - 6 - th >= 0 else y1 + th + 6
        cv2.rectangle(bgr, (x1, ty - th - 3), (x1 + tw + 4, ty + 3),
                      (0, 0, 0), -1)
        cv2.putText(bgr, label, (x1 + 2, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    return bgr


# --------------------------------------------------------------------------- #
# 颜色校正（只对 MIPI 这种没有 AWB 的 raw 源）
# --------------------------------------------------------------------------- #
class ColorBalance:
    """在 BGR 域给 R/B 各乘一个增益，G 不动 —— 和 whitebalance.py 同一套数学。

    whitebalance.py 在 UYVY 域做 3x3 矩阵；detect 模式的帧已经是 BGR，直接在
    BGR 上乘 diag(kr, 1, kb) 完全等价，而且能用 OpenCV 的 LUT 一次查表完成
    （720p 每帧比 float 乘法省一个数量级的 CPU）。

    `auto` 时前 WB_CALIB_FRAMES 帧只统计不校正（和 monitor 一样），算完冻结。
    太暗/过曝就退回 whitebalance.WB_DEFAULT_GAINS —— 偏色方向是传感器的
    光谱响应决定的，和明暗无关，只是暗帧估不准倍数。
    """

    def __init__(self, spec):
        self.auto, fixed = whitebalance.parse_spec(spec)
        self.kr, self.kb = fixed or whitebalance.WB_DEFAULT_GAINS
        self.lut = None
        self._sums = [0.0, 0.0, 0.0]     # R, G, B
        self._clip = 0.0
        self._n = 0
        # 给 /stats 用的一句话状态。自校准完成后必须从"自校准中"变成实际
        # 增益 —— 这也是"到底校没校完"在无头运行时的唯一证据。
        self.desc = "自校准中" if self.auto else "R×%.2f B×%.2f" % (self.kr, self.kb)
        if not self.auto:
            self._build_lut()

    def apply(self, bgr):
        if self.lut is not None:
            return require_cv2().LUT(bgr, self.lut)
        if not self.auto:
            return bgr
        self._accumulate(bgr)
        if self._n >= whitebalance.WB_CALIB_FRAMES:
            self._freeze()
        return bgr

    def _build_lut(self):
        np = require_numpy()
        b = np.clip(np.arange(256) * self.kb, 0, 255).astype(np.uint8)
        g = np.arange(256, dtype=np.uint8)
        r = np.clip(np.arange(256) * self.kr, 0, 255).astype(np.uint8)
        # LUT 的通道顺序必须和图像一致：BGR。
        self.lut = np.stack([b, g, r], axis=-1).reshape(1, 256, 3)

    def _accumulate(self, bgr):
        np = require_numpy()
        b = bgr[..., 0]
        g = bgr[..., 1]
        r = bgr[..., 2]
        self._sums[0] += float(r.mean())
        self._sums[1] += float(g.mean())
        self._sums[2] += float(b.mean())
        # 过曝判据用亮度，和 whitebalance.py 的口径一致。
        y = 0.299 * r.astype(np.float32) + 0.587 * g + 0.114 * b
        self._clip += float((y > 250).mean())
        self._n += 1

    def _freeze(self):
        n = max(self._n, 1)
        r, g, b = (x / n for x in self._sums)
        clip = self._clip / n
        y = 0.299 * r + 0.587 * g + 0.114 * b
        why = None
        if y < whitebalance.WB_MIN_SIGNAL or min(r, g, b) < 4.0:
            why = "画面太暗"
        elif clip > whitebalance.WB_MAX_CLIP:
            why = "画面过曝 %.1f%%（上限 %.0f%%）" % (
                clip * 100, whitebalance.WB_MAX_CLIP * 100)
        else:
            lo, hi = whitebalance.WB_GAIN_RANGE
            kr, kb = g / r, g / b
            if lo <= kr <= hi and lo <= kb <= hi:
                self.kr, self.kb = kr, kb
            else:
                why = "增益超出 %g~%g" % (lo, hi)

        if why:
            self.kr, self.kb = whitebalance.WB_DEFAULT_GAINS
            log.warning(
                "白平衡自校准：前 %d 帧折算出的 R/G/B 均值是 %.1f/%.1f/%.1f、"
                "过曝像素 %.1f%%，%s，改用默认 R×%.2f B×%.2f",
                n, r, g, b, clip * 100, why, *whitebalance.WB_DEFAULT_GAINS)
        else:
            log.info("白平衡自校准（前 %d 帧，灰世界）：均值 R=%.1f G=%.1f B=%.1f、"
                     "过曝 %.1f%% → R×%.3f B×%.3f，之后冻结",
                     n, r, g, b, clip * 100, self.kr, self.kb)
        self.desc = "R×%.2f B×%.2f" % (self.kr, self.kb)
        self._build_lut()


# --------------------------------------------------------------------------- #
# RKNN
# --------------------------------------------------------------------------- #
class RknnRunner:
    """一个 RKNNLite 上下文。**只由 DetectBridge 的线程创建和使用。**

    模型 load/init 约 100 ms（板子上实测），换摄像头时重新建一个完全可以接受；
    换来的是所有权极其简单：谁建谁释放，不需要跨线程传一个 NPU 上下文。
    """

    def __init__(self, model_path=MODEL_DEFAULT, conf=CONF_DEFAULT,
                 iou=IOU_DEFAULT):
        self.model_path = model_path
        self.conf = float(conf)
        self.iou = float(iou)
        self._np = require_numpy()
        self._cv2 = require_cv2()

        from rknnlite.api import RKNNLite
        self._rknn = RKNNLite()
        if self._rknn.load_rknn(model_path) != 0:
            self._rknn = None
            raise RuntimeError("模型加载失败: %s" % model_path)
        core = getattr(RKNNLite, "NPU_CORE_0", None)
        ret = (self._rknn.init_runtime(core_mask=core)
               if core is not None else self._rknn.init_runtime())
        if ret != 0:
            self.release()
            raise RuntimeError("NPU 初始化失败（返回值 %r）" % (ret,))

    def infer(self, bgr):
        """一帧 BGR → (原帧像素框列表, 推理耗时 ms)。"""
        canvas, scale, pad_x, pad_y = letterbox_bgr(bgr)
        # 模型训练时吃 RGB；OpenCV 的帧是 BGR，必须在这里换过来。
        rgb = self._cv2.cvtColor(canvas, self._cv2.COLOR_BGR2RGB)
        t0 = time.perf_counter()
        outputs = self._rknn.inference(inputs=[rgb[self._np.newaxis, ...]])
        ms = (time.perf_counter() - t0) * 1000.0
        h, w = bgr.shape[:2]
        dets = decode(outputs, conf=self.conf, iou=self.iou)
        return to_frame_boxes(dets, scale, pad_x, pad_y, w, h), ms

    def release(self):
        rknn = getattr(self, "_rknn", None)
        self._rknn = None
        if rknn is not None:
            try:
                rknn.release()
            except Exception:            # noqa: BLE001
                log.debug("释放 RKNN 时出错（忽略）", exc_info=True)


# --------------------------------------------------------------------------- #
# 采集段
# --------------------------------------------------------------------------- #
def segment_desc(a, src):
    """可替换的采集段：v4l2src →（MJPG 硬解）→ videoconvert BGR → appsink。

    这一段的终点是 appsink，不是 h264parse —— 因为帧要先经过 DetectBridge
    才能进编码器。`async=false` 是必须的：段是在 PLAYING 的管线里加进来的，
    appsink 默认会等第一帧才完成状态切换，而"等"的那几秒里整条管线的状态
    切换都卡着（detect_canvas 上实测过）。
    """
    head = "v4l2src name=vsrc0 device={dev} ! {caps} ! ".format(
        dev=src.device, caps=P.input_caps(a, src))
    if src.kind == "mjpeg":
        head += "mppjpegdec ! "
    return head + (
        # n-threads=2：720p UYVY/NV12 → BGR 是这条链上最重的 CPU 活之一，
        # 单线程会明显拖慢采集。两线程足够，再多只是在和编码器抢核。
        "videoconvert n-threads=2 ! video/x-raw,format=BGR ! "
        "appsink name=detsink max-buffers=2 drop=true sync=false async=false"
    )


class DetectSegment:
    """一段采集链，只由 DetectSwitch 创建/销毁，**只能在 GLib 线程碰**。"""

    def __init__(self, a, src, log_):
        Gst = require_gst()
        desc = segment_desc(a, src)
        self.bin = Gst.parse_bin_from_description(desc, False)
        if self.bin is None:
            raise RuntimeError("detect 采集段建不出来（源 %s）：%s" % (src.name, desc))
        self.src = src
        self.sink = self.bin.get_by_name("detsink")
        if self.sink is None:
            raise RuntimeError("采集段里没有 detsink（描述串写错了？）：%s" % desc)
        # 拿到的是 GstAppSink 还是光秃秃的 Element，取决于 GstApp 有没有在
        # parse 之前 import。这里再检查一次，给出比 AttributeError 好懂的报错。
        if not hasattr(self.sink, "try_pull_sample"):
            raise RuntimeError(
                "detsink 不是 GstAppSink（GstApp 没在 parse 之前 import？）")

    def teardown(self, pipeline, log_):
        """从运行中的管线上摘掉这一段：NULL → 等真的到 NULL → remove。

        remove() 那一步是项目里 fd 泄漏的老结论：appsink 拉过 sample 之后会
        攥着一个 buffer，buffer 攥着 v4l2 的 pool，pool 攥着设备 fd；只有把段
        从管道上摘掉，这条引用链才会断（详见 pipeline.VideoSwitch.dispose）。
        """
        Gst = require_gst()
        self.bin.set_state(Gst.State.NULL)
        self.bin.get_state(2 * Gst.SECOND)
        P.confirm_null(self.bin, "detect 采集段", log_)
        pipeline.remove(self.bin)


# --------------------------------------------------------------------------- #
# 桥线程：appsink → OpenCV/RKNN → appsrc
# --------------------------------------------------------------------------- #
class DetectBridge:
    """一条 appsink → appsrc 的 Python 桥。

    每次换源/换会话都是**新线程 + 新 RKNN 上下文**。stop() 会 join，线程的
    finally 里释放 RKNN —— 释放发生在真正用过它的那个线程上，避开
    detect_canvas 上那个"在 GLib 线程 release 时段错误"的坑。
    """

    def __init__(self, sink, appsrc, a, src, log_):
        self.sink = sink
        self.appsrc = appsrc
        self.a = a
        self.src = src
        self.log = log_
        self.detect_fps = max(0.1, float(getattr(a, "detect_fps", DETECT_FPS_DEFAULT)))
        self.conf = float(getattr(a, "detect_conf", CONF_DEFAULT))
        self.iou = float(getattr(a, "detect_iou", IOU_DEFAULT))
        self._stop = threading.Event()
        self._thread = None
        self._wb = None
        self._boxes = []
        self._last_infer_at = None
        self._pulled = False
        self._idle = 0

        # 读这些数的是 1Hz 遥测线程；写的人是本线程。普通 int/float，GIL 下足够。
        self.frames = 0              # 推给编码器的帧数
        self.inferences = 0          # 成功推理次数
        self.boxes_total = 0         # 累计检出的人框数
        self.errors = 0
        self.last_ms = 0.0
        self.last_n = 0
        self.last_error = ""
        self.ready = False           # RKNN 是否已加载
        self.wb_desc = ""

    # ---------------- 生命周期 ----------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="detect-bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.5):
        """收线程。调用方要保证在拆 appsink **之前**调。

        appsrc 是 block=false + leaky-type=downstream（见 skeleton_desc），
        所以 push_buffer 不会卡住；线程最坏也只是卡在一次 100 ms 的
        try_pull_sample 或一次推理里，2.5 s 足够。
        """
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                # 没在时限内退出来 = 还卡在拉帧/推理里。往下走就是拆别人正在
                # 用的 appsink，现场只会表现为莫名其妙的段错误 —— 必须喊出来。
                self.log.warning("detect 桥：线程 %.1fs 内没停下", timeout)
        self.sink = None
        self.appsrc = None
        self._wb = None

    def counters(self):
        return {
            "detect_ready": self.ready,
            "detect_frames": self.frames,
            "detect_inferences": self.inferences,
            "detect_boxes": self.boxes_total,
            "detect_errors": self.errors,
            "detect_last_ms": round(self.last_ms, 1),
            "detect_last_n": self.last_n,
            "detect_last_error": self.last_error,
            "detect_fps": round(self.detect_fps, 2),
            "detect_wb": self.wb_desc,
        }

    # ---------------- 线程 ----------------
    def _run(self):
        Gst = require_gst()
        runner = None
        try:
            try:
                runner = RknnRunner(getattr(self.a, "detect_model", MODEL_DEFAULT),
                                    conf=self.conf, iou=self.iou)
                self.ready = True
                self.log.info("detect：NPU 已就绪（模型 %s，推理上限 %g fps）",
                              self.a.detect_model, self.detect_fps)
            except Exception as exc:         # noqa: BLE001
                # 检测起不来**不能**把视频一起拖死：推流仍然照常，只是没框。
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                self.errors += 1
                self.log.error("detect：NPU 不可用，视频照常推流但没有检测框：%s",
                               exc)

            if self.src.kind == "raw" and getattr(self.a, "camera_wb", None):
                try:
                    self._wb = ColorBalance(self.a.camera_wb)
                    self.wb_desc = self._wb.desc
                    self.log.info("detect：白平衡 %s（只对 raw 源）", self.wb_desc)
                except Exception as exc:     # noqa: BLE001
                    self.log.warning("detect：白平衡不可用，画面保持原样：%s", exc)
                    self._wb = None

            while not self._stop.is_set():
                sink = self.sink
                if sink is None:
                    return
                try:
                    sample = sink.try_pull_sample(POLL_NS)
                except Exception:            # noqa: BLE001
                    self.log.warning("detect：拉帧异常，桥线程收工", exc_info=True)
                    return
                if sample is None:
                    # 和 whitebalance 一样：pipeline 进 PLAYING 之前 is_eos()
                    # 可能已经是 True，必须真的拉到过数据之后才信它。
                    if self._pulled and sink.is_eos():
                        self.log.info("detect：appsink 已结束，桥线程收工")
                        return
                    self._idle += 1
                    if self._idle * POLL_NS > IDLE_GIVEUP_NS:
                        self.log.warning(
                            "detect：%d 秒没等到帧，桥线程收工（视频管线继续跑）",
                            IDLE_GIVEUP_NS // (1000 * 1000 * 1000))
                        return
                    continue
                self._idle = 0
                self._pulled = True
                if not self._push(sample, runner, Gst):
                    return
        finally:
            if runner is not None:
                self.log.info("detect：释放 NPU 上下文（线程 %s）",
                              threading.current_thread().name)
                runner.release()

    def _push(self, sample, runner, Gst):
        np = require_numpy()
        cv2 = require_cv2()
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if caps is None or caps.get_size() < 1:
            self._error("appsink 没有 caps")
            return True
        s = caps.get_structure(0)
        fmt = s.get_value("format") if s.has_field("format") else None
        if fmt != "BGR":
            self._error("detect 帧应为 BGR，实际 %r" % (fmt,))
            return True
        width = int(s.get_value("width"))
        height = int(s.get_value("height"))

        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            self._error("buffer 映射失败")
            return True
        try:
            raw = np.frombuffer(info.data, dtype=np.uint8)
            stride = frame_stride(width, height, raw.size)
            if raw.size < stride * height:
                self._error("BGR buffer 尺寸不对：%d 字节，期望至少 %d"
                            "（stride=%d）" % (raw.size, stride * height, stride))
                return True
            frame = (raw[:stride * height].reshape(height, stride)
                     [:, :width * 3].reshape(height, width, 3).copy())
        finally:
            buf.unmap(info)

        changed = False
        if self._wb is not None:
            try:
                frame = self._wb.apply(frame)
                # apply() 内部可能刚好跑完 15 帧校准并冻结；把最新状态同步给
                # /stats，否则它会永远停在"自校准中"。
                self.wb_desc = self._wb.desc
                changed = True
            except Exception as exc:         # noqa: BLE001
                self.log.warning("detect：白平衡处理失败，停用它：%s", exc)
                self._wb = None
                self.wb_desc = "失败后停用"

        now = time.monotonic()
        if runner is not None:
            due = (self._last_infer_at is None or
                   (now - self._last_infer_at) >= 1.0 / self.detect_fps)
            if due:
                self._last_infer_at = now
                try:
                    self._boxes, ms = runner.infer(frame)
                    self.inferences += 1
                    self.last_ms = ms
                    self.last_n = len(self._boxes)
                    self.boxes_total += self.last_n
                except Exception as exc:     # noqa: BLE001
                    self._error("推理失败: %s" % exc)
                    # 保留上一次的框：短暂失败时画面不会突然闪空。

        if self._boxes:
            try:
                draw_boxes(frame, self._boxes)
                changed = True
            except Exception as exc:         # noqa: BLE001
                self._error("画框失败: %s" % exc)

        # 内容没变（没开白平衡、这一帧也没有框）就用原 buffer 直通，省一次
        # 720p 的 memcpy；变了才建新 buffer，并把时间戳原样带过去 ——
        # appsrc 是 do-timestamp=false，它不会替我们补。
        if changed:
            out = Gst.Buffer.new_wrapped(frame.tobytes())
            out.pts, out.dts, out.duration = buf.pts, buf.dts, buf.duration
        else:
            out = buf

        src = self.appsrc
        if src is None:
            return False
        ret = src.push_buffer(out)
        if ret == Gst.FlowReturn.OK:
            self.frames += 1
            return True
        if ret == Gst.FlowReturn.FLUSHING:
            return False                     # 正常收工（管线在往 NULL 走）
        self.log.warning("detect：推帧失败: %s", ret)
        return False

    def _error(self, msg):
        self.errors += 1
        self.last_error = msg
        if self.errors <= 3 or self.errors % 100 == 0:
            self.log.warning("detect：%s（第 %d 次）", msg, self.errors)


# --------------------------------------------------------------------------- #
# 骨架 + 切换
# --------------------------------------------------------------------------- #
def skeleton_desc(a):
    """常驻骨架：appsrc → 硬件编码 → RTP，可选音频分支。

    appsrc 的属性是**实测定下来的**（板子上 10 个 buffer 压进 max-buffers=2）：

        block=false + leaky-type=downstream → 队列恒为 2，push 返回 OK
        block=false + leaky-type=none       → 队列涨到 10（max-buffers 形同虚设）
        block=false + leaky-type=upstream   → 满了返回 EOS（桥会误以为流结束）

    downstream 就是"丢新来的帧"。对实时画面来说这是对的：延迟不涨，视频
    继续走，只是慢一点的时候少一帧。用 block=true 的话，桥线程会卡在
    push_buffer 里，换摄像头时 join 不掉，然后去拆它正在用的 appsink。
    """
    enc = P.encoder_desc(a)
    desc = (
        "webrtcbin name=wbr bundle-policy=max-bundle "
        "appsrc name=detsrc is-live=true format=time do-timestamp=false "
        "block=false leaky-type=downstream max-buffers=2 max-bytes=0 "
        # caps 必须在这里写死：appsrc 默认 ANY，下游拿着 ANY 定不下来，
        # 整条 pipeline 的状态切换会 not-negotiated（和 whitebalance 同一个坑）。
        "caps=video/x-raw,format=BGR,width={w},height={h},framerate={fps}/1 ! "
        # mpph264enc 不吃 BGR，必须转成 NV12；n-threads=2 理由同采集段。
        "videoconvert n-threads=2 ! "
        "{enc} ! h264parse name=vtail ! "
        "{tail}"
    ).format(w=a.width, h=a.height, fps=a.fps, enc=enc, tail=P.video_tail(a))
    # 音频分支和 monitor 模式共用 audio_branch()：build_detect() 后面要按
    # sink_1 链接 `aq`，这里不把它写进描述串就会在 get_by_name 上炸。
    if P.audio_enabled(a):
        desc += " " + P.audio_branch(a)
    return desc


class DetectSwitch:
    """骨架 + 当前采集段。**所有方法都只能在 GLib 线程调用。**

    对外接口和 pipeline.VideoSwitch 对齐（source / attach_first / switch /
    start_bridges / dispose / n_switches / overlay），session.py 不需要知道
    自己拿的是哪一种。
    """

    def __init__(self, pipeline, appsrc, a, log_):
        self.pipeline = pipeline
        self.appsrc = appsrc
        self.a = a
        self.log = log_
        self.current = None          # DetectSegment
        self.bridge = None
        self.n_switches = 0
        self.overlay = None          # detect 模式没有 textoverlay；保留属性给 session
        # 桥是**每段一个**：换摄像头会新建桥，计数从 0 开始。这里把上一代
        # 桥的计数结掉，否则 /stats 里的 detect_frames 会在切换时倒退，
        # 看起来像"检测停了"。只给累计值，不代表当前这代的瞬时值。
        self._totals = {"frames": 0, "inferences": 0, "boxes": 0, "errors": 0}
        self._last = {"ms": 0.0, "n": 0, "error": ""}

    @property
    def source(self):
        return self.current.src if self.current is not None else None

    def attach_first(self, src):
        """接上第一个采集段。**不** sync 状态 —— 由 session 的 PLAYING 带上去。"""
        seg = DetectSegment(self.a, src, self.log)
        self.pipeline.add(seg.bin)
        self.current = seg
        self.log.info("detect 视频源：%s（%s，%s %dx%d@%d）",
                      src.name, src.device, src.fourcc,
                      self.a.width, self.a.height, self.a.fps)

    def start_bridges(self):
        """首段的桥线程。等 pipeline 真的 PLAYING 之后再起（理由同 session）。"""
        self._start_bridge()

    def _start_bridge(self):
        if self.current is None:
            return
        self.bridge = DetectBridge(self.current.sink, self.appsrc,
                                   self.a, self.current.src, self.log)
        self.bridge.start()

    def switch(self, name):
        """换一路摄像头。返回最终生效的源名。"""
        Gst = require_gst()
        src = sources.resolve(name)
        if self.current is not None and self.current.src.name == src.name:
            return src.name          # 已经在这一路上，不做无谓的重建

        # 先造新的再拆旧的：parse 不碰硬件，描述串写错时旧段毫发无损。
        seg = DetectSegment(self.a, src, self.log)

        if self.bridge is not None:
            # 必须在拆旧 appsink **之前**收线程；否则线程还攥着一个 buffer，
            # 拆段之后那个 buffer 会让 v4l2 的 pool 和设备 fd 一起留在原地。
            self.bridge.stop()
            self._absorb()           # 先结账，再换桥（新桥计数从 0 开始）
            self.bridge = None
        old = self.current
        old_src = old.src if old is not None else None
        if old is not None:
            old.teardown(self.pipeline, self.log)

        self.pipeline.add(seg.bin)
        ret = seg.bin.sync_state_with_parent()
        if ret == Gst.StateChangeReturn.FAILURE:
            self.pipeline.remove(seg.bin)
            self.current = None
            self.log.error("detect 采集段进不了 PLAYING（源 %s）", src.name)
            # 和 pipeline.VideoSwitch 一样，别把会话扔在"没有视频源"的状态：
            # 旧段已经 teardown 不能原地复活，只能按旧源重新建一段。
            if old_src is not None:
                try:
                    back = DetectSegment(self.a, old_src, self.log)
                    self.pipeline.add(back.bin)
                    back.bin.sync_state_with_parent()
                    self.current = back
                    self._start_bridge()
                    self.log.warning("detect 新段起不来，已退回 %s", old_src.name)
                except Exception:            # noqa: BLE001
                    self.log.exception("detect 退回 %s 也失败了，现在没有视频源",
                                       old_src.name)
            raise RuntimeError("detect 采集段启动失败（源 %s）" % src.name)

        self.current = seg
        self._start_bridge()
        self.n_switches += 1
        self.log.info("detect 视频源已切到 %s（%s）", src.name, src.device)
        return src.name

    def dispose(self):
        """拆整条管线：NULL → 等 NULL → 收桥 → 摘段（fd 回收的最后一步）。"""
        Gst = require_gst()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_state(2 * Gst.SECOND)
        P.confirm_null(self.pipeline, "整条 detect pipeline", self.log)
        if self.bridge is not None:
            self.bridge.stop()
            self._absorb()
            self.bridge = None
        if self.current is not None:
            self.current.teardown(self.pipeline, self.log)
            self.current = None

    def _absorb(self):
        """把当前桥的计数并进累计值。**必须在桥 stop() 之后调用**，否则
        并进来的是半截账，线程随后的增量就丢了。"""
        if self.bridge is None:
            return
        c = self.bridge.counters()
        self._totals["frames"] += int(c["detect_frames"])
        self._totals["inferences"] += int(c["detect_inferences"])
        self._totals["boxes"] += int(c["detect_boxes"])
        self._totals["errors"] += int(c["detect_errors"])
        self._last["ms"] = float(c["detect_last_ms"])
        self._last["n"] = int(c["detect_last_n"])
        if c["detect_last_error"]:
            self._last["error"] = c["detect_last_error"]

    def detect_counters(self):
        base = {
            "detect_ready": False,
            "detect_frames": self._totals["frames"],
            "detect_inferences": self._totals["inferences"],
            "detect_boxes": self._totals["boxes"],
            "detect_errors": self._totals["errors"],
            "detect_last_ms": self._last["ms"],
            "detect_last_n": self._last["n"],
            "detect_last_error": self._last["error"],
            "detect_fps": round(float(getattr(self.a, "detect_fps",
                                               DETECT_FPS_DEFAULT)), 2),
            "detect_wb": "",
        }
        if self.bridge is not None:
            c = self.bridge.counters()
            base.update(c)
            # 桥给的是这一代的数，加上结掉的旧代才是全程累计。
            base["detect_frames"] += self._totals["frames"]
            base["detect_inferences"] += self._totals["inferences"]
            base["detect_boxes"] += self._totals["boxes"]
            base["detect_errors"] += self._totals["errors"]
        return base


# --------------------------------------------------------------------------- #
# 搭建
# --------------------------------------------------------------------------- #
def build_detect(a, ice, log_, source, on_audio_elements=None):
    """搭出 detect 模式的整条管线，返回 (pipeline, webrtcbin, switch)。

    骨架里的 ICE、sink_0/sink_1 链接、音频回程全部复用 pipeline.py 的实现 ——
    两条模式在 WebRTC 这一层必须完全同源。
    """
    Gst = require_gst()

    # GstApp 必须在 parse 之前 import，否则 get_by_name("detsink") 拿到的是
    # 没有 try_pull_sample 的秃 Element（PyGObject 按类型缓存包装对象）。
    whitebalance.require_app_sink()

    pipeline = Gst.parse_launch(skeleton_desc(a))
    webrtcbin = pipeline.get_by_name("wbr")
    if webrtcbin is None:
        raise RuntimeError("描述串里没找到 webrtcbin，parse_launch 可能失败了")
    appsrc = pipeline.get_by_name("detsrc")
    if appsrc is None:
        raise RuntimeError("描述串里没找到 detsrc（detect 骨架写错了？）")

    P.apply_ice(webrtcbin, ice, log_)

    # sink 编号决定 m-line 顺序，和 monitor 模式保持一致：视频 sink_0、音频 sink_1。
    if not P._link(pipeline, webrtcbin, "vq", "sink_0"):
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("视频分支无法连接到 webrtcbin 的 sink_0")

    if P.audio_enabled(a):
        if not P._link(pipeline, webrtcbin, "aq", "sink_1"):
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("音频分支无法连接到 webrtcbin 的 sink_1")
        P.attach_audio_return(webrtcbin, pipeline, a, log_, on_audio_elements)

    switch = DetectSwitch(pipeline, appsrc, a, log_)
    try:
        switch.attach_first(source)
    except Exception:
        # 采集段建不出来时别把已经到 READY 的管线漏在这儿 —— 它会占着设备节点。
        pipeline.set_state(Gst.State.NULL)
        raise
    return pipeline, webrtcbin, switch
