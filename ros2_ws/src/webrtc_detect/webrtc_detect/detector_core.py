#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detector_core.py —— OpenCV 预处理 + RKNN 推理的纯函数部分（不依赖 ROS）

camera_node 和 detector_node 共用这一份；`tools/check_ros_mode.py` 在宿主机上
也能直接 import 它做单测（不需要 GStreamer、也不需要 NPU）。

    letterbox_bgr   等比缩放 + 灰边 → 640×640
    decode          YOLOv5 三尺度输出 → person 框（anchor 解码 + NMS）
    to_frame_boxes  640 输入坐标 → 原帧像素坐标
    draw_boxes      框烧进 BGR 帧
    ColorBalance    BGR 域的灰世界白平衡（和 whitebalance.py 同一套数学）
    RknnRunner      RKNNLite 上下文（谁建谁释放，只在用它的线程里释放）

模型：板子上现成的 YOLOv5s（.rknn），**不入库**。换模型用参数
`model:=...`；输出布局必须仍是 3 尺度 × 3 anchor × (5+80 类)。
"""

import array
import logging
import time

import whitebalance          # 只用常量（增益范围/校准帧数/默认值），不碰 GStreamer

log = logging.getLogger("detector_core")

MODEL_DEFAULT = "/userdata/rknn_yolov5_demo/model/RK3588/yolov5s-640-640.rknn"

INPUT_SIZE = 640
PERSON_CLASS = 0                 # COCO 的 person
NUM_CLASSES = 80
LETTERBOX_PAD = 114              # YOLOv5 训练时的灰边值，别改成 0

# YOLOv5s 的三组 anchor（stride 8/16/32）。实测过：拿板子上的 bus.jpg 解出来的
# 人框和原图对得上；换 anchor 会整体偏大/偏小。
ANCHORS = (
    ((10, 13), (16, 30), (33, 23)),
    ((30, 61), (62, 45), (59, 119)),
    ((116, 90), (156, 198), (373, 326)),
)
STRIDES = (8, 16, 32)

CONF_DEFAULT = 0.35
IOU_DEFAULT = 0.45
DETECT_FPS_DEFAULT = 5.0         # 推理频率；视频帧率另算，别混

_np = None
_cv2 = None


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
                "缺少 numpy —— 检测节点要用它接帧/画框\n"
                "  板子:  sudo apt install python3-numpy") from None
        _np = numpy
    return _np


def require_cv2():
    global _cv2
    if _cv2 is None:
        try:
            import cv2
        except Exception as exc:         # noqa: BLE001
            # 板子上最容易踩的不是"没装 OpenCV"，而是用户目录里的 numpy 2.x
            # 盖住了系统给 python3-opencv 编的 1.x。那种情况下 import cv2
            # 抛的是 numpy 的 ABI 报错，不是 ImportError。
            hint = ""
            if "numpy" in str(exc).lower():
                hint = ("\n  这是 numpy 版本冲突：用户目录里的 numpy 2.x 盖住了"
                        "系统 numpy 1.x。用 python3 -s 跑，或设 PYTHONNOUSERSITE=1。")
            raise RuntimeError("OpenCV 不可用: %s%s" % (exc, hint)) from None
        _cv2 = cv2
    return _cv2


def check_available(model_path=MODEL_DEFAULT):
    """启动前检查依赖，返回错误串或 None。

    这里**不加载模型**：load/init 会把 NPU 上下文建起来，节点刚起来时还
    没收到画面，没必要占着 NPU。
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
    import os
    if not os.path.isfile(model_path):
        return ("模型文件不存在: %s\n"
                "  用 launch 参数 model:=... 指到实际的 .rknn；模型不入库，"
                "来源见 README 的「ROS 模式」一节" % model_path)
    return None


def image_to_bgr(data, width, height, step, encoding="bgr8"):
    """sensor_msgs/Image 的 data → HxWx3 BGR；返回 (frame, 错误说明)。

    **`step` 不一定是 width*3。** 真出现行尾填充时按 width*3 去 reshape 会把
    整幅图切成斜的 —— 症状是画面像被撕开一样错位，而不是报错。所以先按 step
    切行、再取有效列。rgb8（usb_cam 默认）顺手支持：翻转通道得到新数组。
    """
    np = require_numpy()
    if encoding not in ("bgr8", "rgb8"):
        return None, "不认识的图像编码 %r（只认 bgr8/rgb8）" % (encoding,)
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


def frame_stride(width, height, nbytes):
    """一行占多少字节（BGR）。

    videoconvert 出来的是紧凑 BGR，但行尾可能有对齐填充：一帧 buffer 的大小
    正好是 stride*height，直接除就能反推；除不尽按紧凑算 —— 宁可少一层保险，
    也不能因为推不出来就丢帧。

    **别改用 GstVideo.VideoInfo.from_caps()**：只要它在这个检测线程里被调用过，
    收尾时 rknn.release() 就会在 librknnrt 里段错误（detect_canvas 上实测过）。
    本模块完全不碰 GstVideo。
    """
    compact = int(width) * 3
    height = int(height)
    nbytes = int(nbytes)
    if height > 0 and nbytes % height == 0:
        cand = nbytes // height
        if cand >= compact:
            return cand
    return compact


# --------------------------------------------------------------------------- #
# 发布前的数据转换
# --------------------------------------------------------------------------- #
def as_uint8_array(buf):
    """numpy 帧 → `array('B')`，专给 `sensor_msgs/Image.data` 赋值用。

    **别直接赋 `bytes`**：这个版本的 rclpy 把 bytes 塞进 `uint8[]` 字段是
    **逐元素**转的 —— 720p 一帧（2.7MB）实测 439 ms，而且这 0.44 秒是在发布
    线程里同步花掉的。一秒钟发两帧，采集线程（或订阅回调）被一起拖住，现场
    表现是"话题里几乎没有帧"、日志里一条错误都没有 —— 比报错难查得多。
    换成 `array('B')` 之后同样的赋值 0.3 ms（板子上量的）。

    `frombytes` 走的是 buffer 协议，要求 C 连续：切片出来的视图（比如
    `frame[:, :w]`）要先 `ascontiguousarray`，否则抛
    "ndarray is not C-contiguous"。
    """
    np = require_numpy()
    arr = array.array("B")
    arr.frombytes(np.ascontiguousarray(buf))
    return arr


# --------------------------------------------------------------------------- #
# 采集段（camera_node 用；放这里是为了宿主机单测能覆盖解码器的选择）
# --------------------------------------------------------------------------- #
#: MJPG 源的解码器可选值。camera_node 的 `camera_decoder` 参数就是这两个。
MJPEG_DECODERS = ("sw", "mpp")
#: 默认软件解码 —— 原因见 segment_desc 的注释，不是随手选的。
MJPEG_DECODER_DEFAULT = "sw"


def segment_desc(a, src, decoder=MJPEG_DECODER_DEFAULT):
    """一路采集：v4l2src →（MJPG 解码）→ videoconvert BGR → appsink。

    `decoder` 只对 MJPG 源（USB）有意义，原始源（MIPI）用不上：

      · "sw"  —— `jpegdec`，GStreamer 自带的软件解码，**默认**；
      · "mpp" —— `mppjpegdec`，板子 BSP 的 MPP 硬解。

    为什么默认走软件：这块板子上的 `mppjpegdec`（MPP 14729dd57）解 USB 摄像头的
    720p MJPG **不报错、只是极慢** —— 60 帧 20 秒都出不来（≈1.5 秒/帧），
    而同一台机器上 `v4l2src` 本身能给 16 fps、换成 `jpegdec` 就是 60 帧 3.8 秒。
    慢得一条错误日志都没有，现场表现只是"画面卡成幻灯片"，很难查。软件解码在
    8 核上大约吃掉一个核；换了板子/摄像头发现 MPP 正常了，`camera_decoder:=mpp`
    能把这一个核省回来。
    """
    if decoder not in MJPEG_DECODERS:
        raise ValueError("未知的 MJPG 解码器 %r；可选：%s"
                         % (decoder, ", ".join(MJPEG_DECODERS)))

    import pipeline as P        # 惰性 import：detector_node 不需要它

    head = "v4l2src name=csrc device={dev} ! {caps} ! ".format(
        dev=src.device, caps=P.input_caps(a, src))
    if src.kind == "mjpeg":
        head += "mppjpegdec ! " if decoder == "mpp" else "jpegdec ! "
    return head + (
        # n-threads=2：720p UYVY/NV12 → BGR 是这条链上最重的 CPU 活之一，
        # 单线程会明显拖慢采集。
        "videoconvert n-threads=2 ! video/x-raw,format=BGR ! "
        "appsink name=capsink max-buffers=2 drop=true sync=false async=false")


# --------------------------------------------------------------------------- #
# 预处理 / 后处理
# --------------------------------------------------------------------------- #
def letterbox_bgr(bgr, size=INPUT_SIZE, pad_value=LETTERBOX_PAD):
    """等比缩放并补边到 size×size，返回 (canvas, scale, pad_x, pad_y)。

    补灰边而不是拉伸：YOLOv5 训练时就是这么做的，硬拉会把人的比例弄坏。
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

    每个输出是 (1, 3*(5+80), h, w) 或 (3*(5+80), h, w)。模型已经被 rknnlite
    反量化成 float32，值在 0~1（sigmoid 已经做过），所以这里**不再**做
    sigmoid/zero-point，只做 anchor 解码：

        cx = (x*2 - 0.5 + grid_x) * stride
        w  = (w*2)^2 * anchor_w

    框坐标是 640×640 输入空间的像素；调用方用 letterbox 参数映射回原帧。
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
                "换模型时要么保持 YOLOv5 布局，要么改 decode()" % (a.shape,))
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

    返回 [(x1, y1, x2, y2, score), ...]，整数像素，直接交给 draw_boxes。
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

    标签只写 ASCII —— 板子上没有中文字体，写中文会变成一排方框。
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

    白平衡桥在 UYVY 域做 3x3 矩阵；这里的帧已经是 BGR，直接在 BGR 上乘
    diag(kr, 1, kb) 完全等价，而且能用 OpenCV 的 LUT 一次查表完成
    （720p 每帧比 float 乘法省一个数量级的 CPU）。

    `auto` 时前 WB_CALIB_FRAMES 帧只统计不校正（和 monitor 一样），算完冻结。
    太暗/过曝就退回 whitebalance.WB_DEFAULT_GAINS —— 偏色方向是传感器的光谱
    响应决定的，和明暗无关，只是暗帧估不准倍数。
    """

    def __init__(self, spec):
        self.auto, fixed = whitebalance.parse_spec(spec)
        self.kr, self.kb = fixed or whitebalance.WB_DEFAULT_GAINS
        self.lut = None
        self._sums = [0.0, 0.0, 0.0]     # R, G, B
        self._clip = 0.0
        self._n = 0
        # 给 /stats 和日志用的一句话状态。自校准完成后必须从"自校准中"变成
        # 实际增益 —— 这是无头运行时"到底校没校完"的唯一证据。
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
    """一个 RKNNLite 上下文。**只由 detector_node 的推理线程创建和使用。**

    模型 load/init 约 100 ms（板子上实测）。谁建谁释放 —— 释放必须发生在
    真正用过它的那个线程上（在别的线程 release 会段错误，detect_canvas 上
    踩过）。
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
