#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect.py —— 人像检测（板端 RK3588 NPU）

这条链路只在**板子**上跑，浏览器不做任何推理：

    camera → …（编码前的原始帧）→ tee → valve → videorate
           → videoscale(640x360) → videoconvert(RGB) → appsink
           → 本文件的检测线程 → RKNN YOLOv5s → NMS
           → session 把结果经 DataChannel 发给页面，页面用 canvas 画框

为什么放在板端、而不是浏览器里用 TF.js/MediaPipe：
  · 板子有 NPU（rknn-toolkit-lite2 已装），720p 下 5fps 推理只用单核约 15%；
  · 观看端可能是手机/弱机，不该把推理压到它身上；
  · 局域网页面是纯静态单文件，不为检测再引一套前端模型依赖。

模型不是本仓库的一部分（8 MB 权重，且 YOLOv5 权重是 AGPL），默认路径见
MODEL_DEFAULT；换模型用 --detect-model。模型/运行时缺失时，本模块只返回
一个明确的原因，**绝不能让视频流跟着起不来**。

本文件刻意把「纯 numpy 的预处理/后处理」和「GStreamer/RKNN 的线程」分开：
tools/check_detect.py 在宿主机上不需要 GStreamer 也能验证前者。
"""

import logging
import os
import threading
import time

log = logging.getLogger("detect")

#: 板子上现成的 YOLOv5s。换成别的模型时，输出布局必须仍是
#: 3 个尺度 × 3 anchor × (5+80 类)，否则 decode() 会明确报错。
MODEL_DEFAULT = "/userdata/rknn_yolov5_demo/model/RK3588/yolov5s-640-640.rknn"

INPUT_SIZE = 640
PERSON_CLASS = 0                 # COCO 的 person
NUM_CLASSES = 80

# YOLOv5s 的三组 anchor（stride 8/16/32）。这三组数不是猜的：拿板子上的
# bus.jpg 解出来的人框和原图对得上；换 anchor 会整体偏大/偏小。
ANCHORS = (
    ((10, 13), (16, 30), (33, 23)),
    ((30, 61), (62, 45), (59, 119)),
    ((116, 90), (156, 198), (373, 326)),
)
STRIDES = (8, 16, 32)

CONF_DEFAULT = 0.35
NMS_IOU_DEFAULT = 0.45
LETTERBOX_PAD = 114             # YOLO 的惯性补边值（灰），别改成 0

#: 拉帧超时。够短，换摄像头/关会话时线程能及时退出，不会把 GLib 线程卡住。
POLL_NS = 100 * 1000 * 1000

_np = None
_Gst = None
_GstApp = None


# --------------------------------------------------------------------------- #
# 惰性依赖
# --------------------------------------------------------------------------- #
def require_numpy():
    """惰性 import numpy：不开检测的会话不该因为缺它就跑不起来。"""
    global _np
    if _np is None:
        try:
            import numpy
        except ImportError:
            raise RuntimeError(
                "缺少 numpy —— 人像检测要用它做预处理/后处理\n"
                "  板子:  sudo apt install python3-numpy\n"
                "  其它:  pip3 install numpy"
            ) from None
        _np = numpy
    return _np


def require_gst():
    """惰性拿 Gst（要 Gst.MapFlags）。"""
    global _Gst
    if _Gst is None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        _Gst = Gst
    return _Gst


def prepare_appsink():
    """建 pipeline 之前调一次：把 GstApp import 进来。

    和 whitebalance.require_app_sink() 是同一个坑：PyGObject 按 GObject 类型缓存
    包装对象。GstApp 没 import 时 `pipeline.get_by_name("detsink")` 拿到的是个
    光秃秃的 Gst.Element，之后 import 也换不回来，症状是
    `'GstElement' object has no attribute 'try_pull_sample'`。

    **必须在 pipeline.parse 之前调**。这个方法不碰 numpy/rknn，检测不可用时
    也可以安全调（只是让包装类型正确）。
    """
    global _GstApp
    if _GstApp is None:
        try:
            import gi
            gi.require_version("GstApp", "1.0")
            from gi.repository import GstApp
        except (ValueError, ImportError):
            raise RuntimeError(
                "缺少 GstApp（appsink 的 Python 绑定）\n"
                "  板子:  sudo apt install gir1.2-gst-plugins-base-1.0"
            ) from None
        _GstApp = GstApp
    return _GstApp


def check_available(model_path=MODEL_DEFAULT):
    """启动前检查依赖，返回错误串或 None（和 whitebalance.check_available 同款）。

    这里**不加载模型**，只确认门都开着；真正 load/init 在 Detector 里做，
    因为那会把 NPU 上下文建起来，不该在没有观看端、也没开检测时就占着。
    """
    try:
        import numpy  # noqa: F401
    except ImportError:
        return "缺少 numpy（sudo apt install python3-numpy）"
    try:
        from rknnlite.api import RKNNLite  # noqa: F401
    except ImportError:
        return ("缺少 rknn-toolkit-lite2（板子厂商镜像通常自带；"
                "没有就按 Rockchip 的 rknn-toolkit2 装 Python 绑定）")
    if not os.path.isfile(model_path):
        return ("模型文件不存在: %s\n"
                "  用 --detect-model 指到实际的 .rknn；模型不入库，"
                "来源见 README 的「人像检测」一节" % model_path)
    return None


# --------------------------------------------------------------------------- #
# 纯 numpy：预处理 / 后处理（宿主机可单测）
# --------------------------------------------------------------------------- #
def frame_stride(width, height, nbytes):
    """一行占多少字节（RGB）。

    videoconvert 出来的是紧凑 RGB，但行尾可能有对齐填充：stride 往往是
    「宽度*3 按 4 字节向上对齐」而不是宽度*3。一帧 buffer 的大小正好是
    stride*height，所以直接除就能反推；除不尽就按紧凑算 —— 宁可少一层
    保险，也不能因为推不出来就丢帧。

    **别改用 `GstVideo.VideoInfo.from_caps()`**（这是板子上实测出来的坑，
    排查花了一整轮）：只要它在这个检测线程（非 GLib 线程）里被调用过，
    会话收尾时 `rknn.release()` 就会在 librknnrt 里段错误。faulthandler
    抓到的现场是 `rknn_lite.py:release ← detect.release ←
    session._stop_on_glib`，看起来像 NPU 的问题，实际凶手是前面那个
    GStreamer 调用。可证伪的边界：同一个调用放在主线程调 40 次完全没事；
    检测线程里就算一帧都不推理、只调它，收尾照样炸。
    顺带一个旧坑：`from_caps` 是返回 (ok, info) 的类方法，不是填调用者
    对象的实例方法 —— 写成 `vi.from_caps(caps); vi.stride[0]` 不报错，
    只会拿到 0。
    """
    compact = int(width) * 3
    height = int(height)
    nbytes = int(nbytes)
    if height > 0 and nbytes % height == 0:
        cand = nbytes // height
        if cand >= compact:
            return cand
    return compact


def letterbox(rgb, size=INPUT_SIZE, pad_value=LETTERBOX_PAD):
    """等比缩放并补边到 size×size。返回 (canvas, scale, pad_x, pad_y)。

    补的是灰边而不是拉伸：YOLOv5 训练时就是这么做的，硬拉会把人的比例弄坏。
    缩放用最近邻 —— 5fps 的检测输入，插值质量不是瓶颈，省掉一次重采样更简单。
    """
    np = require_numpy()
    rgb = np.asarray(rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("期望 HxWx3 的 RGB，实际 shape=%r" % (rgb.shape,))
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h <= 0 or w <= 0:
        raise ValueError("帧尺寸非法: %dx%d" % (w, h))

    scale = min(float(size) / w, float(size) / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    if (nw, nh) != (w, h):
        # 输出像素 (i,j) 对应输入像素 (i/scale, j/scale)。整数索引 = 最近邻。
        yi = np.clip((np.arange(nh) / scale).astype(np.int32), 0, h - 1)
        xi = np.clip((np.arange(nw) / scale).astype(np.int32), 0, w - 1)
        rgb = rgb[yi][:, xi]

    canvas = np.full((size, size, 3), pad_value, dtype=np.uint8)
    pad_x = (size - nw) // 2
    pad_y = (size - nh) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = rgb
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


def nms(dets, iou=NMS_IOU_DEFAULT):
    """类别无关的 NMS。这里只留 person 一类，所以不需要按类分开做。"""
    ordered = sorted(dets, key=lambda d: d[0], reverse=True)
    keep = []
    for d in ordered:
        if all(_iou(d, k) <= iou for k in keep):
            keep.append(d)
    return keep


def decode(outputs, conf=CONF_DEFAULT, iou=NMS_IOU_DEFAULT):
    """YOLOv5 三尺度输出 → person 框列表。

    每个输出是 (1, 3*(5+80), h, w) 或 (3*(5+80), h, w)。板子上的模型已经被
    rknnlite 反量化成 float32，值在 0~1（sigmoid 已经做过），所以这里**不再**
    做 sigmoid/zero-point，只做 YOLOv5 的 anchor 解码：

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
        box = [None] * len(ks)
        for i in range(len(ks)):
            box[i] = (float(score[ks[i], ys[i], xs[i]]), PERSON_CLASS,
                      float(cx[i] - bw[i] / 2.0), float(cy[i] - bh[i] / 2.0),
                      float(bw[i]), float(bh[i]))
        dets.extend(box)
    return nms(dets, iou)


def normalize(dets, scale, pad_x, pad_y, frame_w, frame_h):
    """把 640×640 输入坐标映射回原帧，再归一化成 0~1。

    归一化是页面叠加层能对准的前提：页面只知道"框在画面的百分之几"，
    不需要知道摄像头是 720p 还是 1080p，也不受 CSS 显示尺寸影响。
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
        out.append([
            round(x1 / frame_w, 4),
            round(y1 / frame_h, 4),
            round((x2 - x1) / frame_w, 4),
            round((y2 - y1) / frame_h, 4),
            round(score, 3),
        ])
    return out


def person_boxes(outputs, frame_w, frame_h, scale, pad_x, pad_y,
                 conf=CONF_DEFAULT, iou=NMS_IOU_DEFAULT):
    """一步到位：解码 + NMS + 归一化。"""
    return normalize(decode(outputs, conf=conf, iou=iou),
                     scale, pad_x, pad_y, frame_w, frame_h)


def too_soon(last_at, now, fps, ratio=0.9):
    """检测线程的兜底限流判据：距上一帧推理太近就跳过。

    `last_at` / `now` 都是 `time.monotonic()` 的读数。留 10% 余量是给
    videorate 的正常抖动（实测它偶尔会出 6 帧/秒），不至于把本该处理的帧也丢掉。

    **为什么检测线程要自己限流**：限流本来只靠管线里的 videorate。但采集段
    重建之后（换摄像头、以及页面重连后新建会话）它会有一段时间不按 fps 丢帧
    —— 实测切源后整整 19 秒按采集帧率（~32/s）全放行，然后才回到 5/s。这段时间
    NPU 的占用是配置值的 6 倍。与其和 GStreamer 的协商时机较劲，不如按墙钟
    时间兜底：appsink 是 max-buffers=1 drop=true，多拉的那几帧丢掉不积压。
    """
    if last_at is None:
        return False
    return (now - last_at) < ratio / max(1, int(fps))


# --------------------------------------------------------------------------- #
# RKNN + appsink 线程
# --------------------------------------------------------------------------- #
class DetectUnavailable(RuntimeError):
    """模型/运行时不可用。调用方把它变成页面上的禁用态，不打断视频。"""


class Detector:
    """持有 RKNN 上下文，并从 appsink 拉帧做检测。

    一台板子同一时刻只有一个会话、一个检测线程（本项目的单观看端设计），
    所以这里不做多实例共享，也不给 inference 加锁。切换摄像头时由 session
    先 stop() 再重新 start(新 sink) —— 旧 appsink 必须在旧段被 NULL 之前松手。
    """

    def __init__(self, model_path=MODEL_DEFAULT, fps=5,
                 conf=CONF_DEFAULT, iou=NMS_IOU_DEFAULT):
        self.model_path = model_path
        self.fps = max(1, int(fps))
        self.conf = float(conf)
        self.iou = float(iou)

        self._np = require_numpy()
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise DetectUnavailable(
                "缺少 rknn-toolkit-lite2 —— 检测要用它跑 NPU") from exc
        if not os.path.isfile(model_path):
            raise DetectUnavailable("模型文件不存在: %s" % model_path)

        self._rknn = RKNNLite()
        if self._rknn.load_rknn(model_path) != 0:
            raise DetectUnavailable("模型加载失败: %s" % model_path)
        core = getattr(RKNNLite, "NPU_CORE_0", None)
        ret = (self._rknn.init_runtime(core_mask=core)
               if core is not None else self._rknn.init_runtime())
        if ret != 0:
            try:
                self._rknn.release()
            except Exception:                # noqa: BLE001
                pass
            raise DetectUnavailable("NPU 初始化失败（返回值 %r）" % (ret,))

        self._stop = threading.Event()
        self._thread = None
        self._sink = None
        self._on_result = None
        self._pulled = False

        # 读这些数的是 session 的 1Hz 遥测（asyncio 线程）；写的人是检测线程。
        # 都是普通 int/float，和项目里其它计数器一样，GIL 下不加锁。
        self.frames = 0
        self.errors = 0
        self.last_ms = 0.0
        self.last_n = 0
        self.last_ts = 0.0
        self.last_error = ""
        # 兜底限流丢掉的上游帧数，以及上一帧推理的墙钟时刻（见 too_soon）。
        self.skipped = 0
        self._last_infer_at = None

    # ---------------- 生命周期 ----------------
    def start(self, sink, on_result):
        """从 sink 拉帧。会先停掉当前线程（换摄像头时就是靠这个换 sink）。"""
        self.stop()
        self._sink = sink
        self._on_result = on_result
        self._pulled = False
        self._last_infer_at = None       # 新 sink/新采集段，限流从头算
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="person-detect", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        """收线程。**调用方要保证在拆 appsink 之前调**，否则旧 buffer 会拖住
        旧采集段的 buffer pool（正是项目里 fd 泄漏那条已知链路）。"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                # 没在时限内退出来 = 还卡在拉帧或推理里。这时候往下走就是
                # 在拆别人正在用的东西（NPU 上下文 / appsink），现场只会
                # 表现为进程莫名其妙地段错误 —— 必须喊出来。
                log.warning("检测：线程 %.1fs 内没停下，仍在拉帧/推理", timeout)
        self._sink = None
        self._on_result = None

    def release(self):
        """放掉 NPU 上下文。会话结束时调一次。"""
        self.stop()
        rknn = self._rknn
        self._rknn = None
        if rknn is not None:
            log.info("检测：释放 NPU 上下文（线程 %s）",
                     threading.current_thread().name)
            try:
                rknn.release()
            except Exception:                # noqa: BLE001
                log.debug("释放 RKNN 时出错（忽略）", exc_info=True)
            else:
                log.info("检测：NPU 上下文已释放")

    def stats(self):
        return {
            "detect_frames": self.frames,
            "detect_errors": self.errors,
            "detect_skipped": self.skipped,
            "detect_last_ms": round(self.last_ms, 1),
            "detect_last_n": self.last_n,
            "detect_last_ts": round(self.last_ts, 3),
            "detect_last_error": self.last_error,
        }

    # ---------------- 线程 ----------------
    def _run(self):
        Gst = require_gst()
        while not self._stop.is_set():
            sink = self._sink
            if sink is None:
                return
            try:
                sample = sink.try_pull_sample(POLL_NS)
            except Exception:                # noqa: BLE001
                # 拉帧出错 = 这个线程到此为止。静默退出是最糟的形态：
                # 页面上框没了、frames 不涨，日志里却什么都没有。
                log.warning("检测：拉帧异常，线程收工", exc_info=True)
                return
            if sample is None:
                # 和 whitebalance 一样：pipeline 进 PLAYING 之前 is_eos() 可能
                # 已经是 True，必须真正拉到过数据之后才信它。
                if self._pulled and sink.is_eos():
                    log.info("检测：appsink 已结束，线程收工")
                    return
                continue
            self._pulled = True

            # 兜底限流（判据和原因见 too_soon）。只管推理，拉帧照拉 ——
            # appsink 是 max-buffers=1 drop=true，被丢的帧不会积压。
            now = time.monotonic()
            if too_soon(self._last_infer_at, now, self.fps):
                self.skipped += 1
                if self.skipped == 1 or self.skipped % 500 == 0:
                    log.warning(
                        "检测：上游出的帧快过 %d fps（距上一帧只有 %.0f ms），"
                        "检测线程按配置限流（累计丢 %d 帧）",
                        self.fps, (now - self._last_infer_at) * 1000,
                        self.skipped)
                continue
            self._last_infer_at = now

            try:
                result = self._infer(sample, Gst)
            except Exception as exc:         # noqa: BLE001
                self.errors += 1
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                if self.errors <= 3 or self.errors % 100 == 0:
                    log.warning("人像检测推理失败（第 %d 次）：%s",
                                self.errors, exc)
                continue

            self.frames += 1
            self.last_ms = result["ms"]
            self.last_n = result["n"]
            self.last_ts = result["ts"]
            callback = self._on_result
            if callback is not None:
                try:
                    callback(result)
                except Exception:            # noqa: BLE001
                    log.exception("人像检测：结果回调失败（不影响后续帧）")

    def _infer(self, sample, Gst):
        np = self._np
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if caps is None or caps.get_size() < 1:
            raise RuntimeError("appsink 没有 caps")
        s = caps.get_structure(0)
        fmt = s.get_value("format") if s.has_field("format") else None
        if fmt != "RGB":
            raise RuntimeError("检测帧应为 RGB，实际 %r" % (fmt,))
        width = int(s.get_value("width"))
        height = int(s.get_value("height"))

        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("buffer 映射失败")
        try:
            raw = np.frombuffer(info.data, dtype=np.uint8)
            # 行尾可能有对齐填充：按 stride 切行，再裁到 width*3。
            # 反推逻辑和那个 GstVideo 的坑见 frame_stride() 的注释。
            stride = frame_stride(width, height, raw.size)
            if raw.size < stride * height:
                raise RuntimeError(
                    "RGB buffer 尺寸不对：%d 字节，期望至少 %d（stride=%d）"
                    % (raw.size, stride * height, stride))
            rgb = (raw[:stride * height].reshape(height, stride)
                   [:, :width * 3].reshape(height, width, 3).copy())
        finally:
            buf.unmap(info)

        canvas, scale, pad_x, pad_y = letterbox(rgb)
        t0 = time.perf_counter()
        outputs = self._rknn.inference(inputs=[canvas[np.newaxis, ...]])
        ms = (time.perf_counter() - t0) * 1000.0
        boxes = person_boxes(outputs, width, height, scale, pad_x, pad_y,
                             conf=self.conf, iou=self.iou)
        return {
            "t": "det",
            "on": True,
            "ts": round(time.time(), 3),
            "ms": round(ms, 1),
            "n": len(boxes),
            "boxes": boxes,
        }
