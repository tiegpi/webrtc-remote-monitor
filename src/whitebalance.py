#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whitebalance.py —— 摄像头画质兜底（偏绿这一半）

和 camera.py 是**同一个根因**：板子上没有 rkaiq 3A 服务，AWB 不跑，ISP 就把
OV13850 的原始 Bayer 直接转成 YUV —— 没有白平衡。表现是画面能看、帧率正常、
ICE 也连上了，但整体发绿，而且**调增益解决不掉**：增益只改亮度，偏色是红蓝
相对绿缺了一截。

实测（当前这间屋子、gain=248、一帧 UYVY 折算）：R/G/B = 54.6/93.4/60.3，
G/R=1.71、G/B=1.55。缺的这一步在数学上很干净：**给 R 和 B 各乘一个增益、G 不动**，
灰世界假设下 kr = G/R、kb = G/B。

**kr/kb 只有在校准帧本身曝光正常时才算得准**，而且实测这个估计值会随亮度变
（同一间屋子，gain 从 16 涨到 248，G/R 从 1.32 一路读到 1.71 —— 暗帧的蓝通道
贴着噪声底，读小了）。所以：自校准会拒绝太暗/过曝的帧，退回默认增益；
改完曝光（camera.py 那套）必须重开推流端重新校准。

为什么不用现成元素：
  · videobalance 的 hue/saturation 是绕亮度轴**旋转/缩放**色度，而这里要的是
    一个色度**平移** —— 旋转做不到平移，实测消不掉。
  · colorchannelmixer 一行就够，但它在 avfilter 里，板子上没装 gst-libav
    （Debian 的 gst-libav 不带它）。
  · 写 Gst.Element 子类要 python3-gst-1.0，板子上只有 gir1.2-* 的 typelib，
    Gst.Element.register() 直接返回 False。
所以只能自己算：appsink → numpy → appsrc，只用现成元素。

代价是每帧两次拷贝加一趟 numpy —— 720p 实测多花约 0.4 个核，**帧率不变**
（这条路的瓶颈是 ISP 的 ~20fps，不是 CPU）。
"""

import logging
import re
import threading

log = logging.getLogger("wb")

# 默认增益：只在自校准失败（太暗/过曝/越界）时才会用到。
# 这组是**当前这间屋子**实测的（gain=248、Y=78），换个房间/换个光照就会偏。
WB_DEFAULT_GAINS = (1.71, 1.55)
WB_GAIN_RANGE = (0.5, 4.0)      # 自校准结果超出这个范围就当没算出来
WB_CALIB_FRAMES = 15            # 灰世界估计用前多少帧（720p@20 ≈ 0.75 秒）
WB_MIN_SIGNAL = 24.0            # 折算出的 Y 均值低于这个值就不信这次估计
WB_MAX_CLIP = 0.05              # 亮到 250 以上的像素超过这个比例，估计也作废

WB_POLL_NS = 100 * 1000 * 1000          # 桥线程轮询 appsink 的超时
WB_IDLE_GIVEUP_NS = 5 * 1000 * 1000 * 1000   # 一直没数据就收工，别把线程挂着

# 桥的两头。中间是**空的**：parse_launch 会把它们当成两条互不相连的 chain，
# 接起来的是 WhiteBalanceBridge 里的线程。
WB_SINK_DESC = "appsink name=wbsink max-buffers=2 drop=true sync=false "
# appsrc 的 caps 必须**在这里就写死**，不能等第一帧推上去再设：两条 chain 是分开
# 的状态机，但整条 pipeline 要一起进 PAUSED/PLAYING，而 appsrc 的 caps 默认是 ANY，
# 下游拿着 ANY 定不下来 → 状态切换直接 not-negotiated，第一帧永远等不到（实测报错
# 落在 wbsrc 上，很容易误判成摄像头协商失败）。宽高帧率不用猜：摄像头分支已经把
# width/height/framerate 钉死在 v4l2src 的 caps 里，中间没人改尺寸。
WB_SRC_DESC = (
    "appsrc name=wbsrc is-live=true format=time do-timestamp=false "
    "block=true max-buffers=2 max-bytes=0 caps={caps} ! "
)

_np = None
_GstApp = None
_Gst = None
_wb_matrices = None


# --------------------------------------------------------------------------- #
# 惰性依赖
# --------------------------------------------------------------------------- #
def require_numpy():
    """惰性 import numpy：不用 --camera-wb 的人不该因为缺它就跑不起来。"""
    global _np
    if _np is None:
        try:
            import numpy
        except ImportError:
            raise RuntimeError(
                "缺少 numpy —— 白平衡桥要用它做逐像素校正\n"
                "  板子:  sudo apt install python3-numpy\n"
                "  其它:  pip3 install numpy"
            ) from None
        _np = numpy
    return _np


def require_gst():
    """惰性拿 Gst（要 Gst.Buffer / Gst.FlowReturn）。"""
    global _Gst
    if _Gst is None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        _Gst = Gst
    return _Gst


def require_app_sink():
    """appsink/appsrc 的方法绑在 GstApp 命名空间里。

    GstApp 属于 gstreamer1.0-plugins-base，正常情况下都在；做成惰性只是和 numpy
    一样：不加 --camera-wb 就不碰它。

    **必须在 Gst.parse_launch() 之前调。** PyGObject 是按 GObject 实例的类型缓存
    包装对象的：GstApp 没 import 时 `pipeline.get_by_name("wbsink")` 拿到的是一个
    光秃秃的 `Gst.Element`，之后再 import GstApp 也换不回来 —— 症状是
    `'GstAppSink' object has no attribute 'try_pull_sample'`（实测踩过），
    看起来像 GStreamer 版本不对，其实是 import 顺序。
    """
    global _GstApp
    if _GstApp is None:
        try:
            import gi
            gi.require_version("GstApp", "1.0")
            from gi.repository import GstApp
        except (ValueError, ImportError):
            raise RuntimeError(
                "缺少 GstApp（appsink/appsrc 的 Python 绑定）\n"
                "  板子:  sudo apt install gir1.2-gst-plugins-base-1.0"
            ) from None
        _GstApp = GstApp
    return _GstApp


def prepare():
    """建 pipeline 之前调一次：把 numpy 和 GstApp 都 import 进来。

    GstApp 必须赶在 parse_launch 之前（理由见 require_app_sink）。依赖缺失时抛
    RuntimeError —— 调用方在启动阶段就拦下来，别等到每来一个观看者报一次。
    """
    require_numpy()
    require_app_sink()


def check_available():
    """启动前检查依赖，返回错误串或 None。"""
    try:
        prepare()
    except RuntimeError as exc:
        return str(exc)
    return None


# --------------------------------------------------------------------------- #
# 数学
# --------------------------------------------------------------------------- #
def matrices():
    """RGB↔YUV 的 3x3（**全范围 BT.601**）及其逆。惰性建，理由同 require_numpy()。

    用全范围 BT.601 是因为实测这条链上标出来的 colorimetry 就是 `1:3:5:1`。
    """
    global _wb_matrices
    if _wb_matrices is None:
        np = require_numpy()
        a = np.array(
            [
                [0.299, 0.587, 0.114],
                [-0.168736, -0.331264, 0.5],
                [0.5, -0.418688, -0.081312],
            ],
            np.float32,
        )
        _wb_matrices = (a, np.linalg.inv(a))
    return _wb_matrices


def matrix_for(kr, kb):
    """作用在 (Y, Cb-128, Cr-128) 上的 3x3，等价于在 RGB 上乘 diag(kr, 1, kb)。"""
    np = require_numpy()
    a, ainv = matrices()
    return (a @ np.diag([kr, 1.0, kb]).astype(np.float32) @ ainv).astype(np.float32)


def correct_uyvy(data, m):
    """把一帧 UYVY 按矩阵 m 校正，返回新的 bytes。

    UYVY 的排布是 [Cb, Y0, Cr, Y1]（色度在前），而且色度是水平 2:1 抽样的：
    一个色度样本管两个像素。所以色度那两项要用这一对的**平均亮度** —— 拿单个
    像素的 Y 去算，等于把色度算错了半个像素。
    """
    np = require_numpy()
    u8 = np.frombuffer(data, np.uint8).reshape(-1, 4)
    c1 = u8[:, 0].astype(np.float32) - 128.0
    c2 = u8[:, 2].astype(np.float32) - 128.0
    y0 = u8[:, 1].astype(np.float32)
    y1 = u8[:, 3].astype(np.float32)
    ya = (y0 + y1) * 0.5
    out = np.empty((u8.shape[0], 4), np.float32)
    out[:, 0] = m[1, 0] * ya + m[1, 1] * c1 + m[1, 2] * c2 + 128.0
    out[:, 2] = m[2, 0] * ya + m[2, 1] * c1 + m[2, 2] * c2 + 128.0
    out[:, 1] = m[0, 0] * y0 + m[0, 1] * c1 + m[0, 2] * c2
    out[:, 3] = m[0, 0] * y1 + m[0, 1] * c1 + m[0, 2] * c2
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8).reshape(-1).tobytes()


def gray_world_gains(mean_y, mean_cb, mean_cr, clip=0.0):
    """灰世界：让三通道均值相等（G 不动）→ ((kr, kb), (r, g, b), 拒绝原因)。

    画面太暗、或者过曝时，均值都不能代表这个场景真实的颜色，硬算出来的增益会把
    颜色推得更歪，所以这两种情况返回 None、第三个值说明是哪种，调用方退回
    WB_DEFAULT_GAINS。退回是对的：这个偏色来自传感器本身的光谱响应（绿像素的
    灵敏度就是比红蓝高一截），和明暗无关，所以**方向永远一样**，只是暗帧/白帧
    估不出准确的倍数。
    """
    np = require_numpy()
    _a, ainv = matrices()
    r, g, b = (float(x) for x in ainv @ np.array([mean_y, mean_cb, mean_cr], np.float64))
    if mean_y < WB_MIN_SIGNAL or min(r, g, b) < 4.0:
        return None, (r, g, b), "画面太暗"
    if clip > WB_MAX_CLIP:
        return None, (r, g, b), "画面过曝 %.1f%%（上限 %.0f%%）" % (
            clip * 100, WB_MAX_CLIP * 100)
    kr, kb = g / r, g / b
    lo, hi = WB_GAIN_RANGE
    if not (lo <= kr <= hi and lo <= kb <= hi):
        return None, (r, g, b), "增益超出 %g~%g" % (lo, hi)
    return (kr, kb), (r, g, b), None


def parse_spec(text):
    """--camera-wb 的取值 → (auto, gains)。

    auto（也是不带值时用的）/ "R,B" 两个增益。不合法时抛 ValueError。
    """
    t = (text or "").strip()
    if t == "" or t.lower() == "auto":
        return True, None
    parts = [x for x in re.split(r"[,:/\s]+", t) if x]
    if len(parts) != 2:
        raise ValueError("--camera-wb 只认 auto 或者 \"R,B\" 两个增益，收到 %r" % text)
    try:
        kr, kb = float(parts[0]), float(parts[1])
    except ValueError:
        raise ValueError("--camera-wb 的增益得是数字，收到 %r" % text) from None
    lo, hi = WB_GAIN_RANGE
    if not (lo <= kr <= hi and lo <= kb <= hi):
        raise ValueError("--camera-wb 的增益要在 %g~%g 之间，收到 %r" % (lo, hi, text))
    return False, (kr, kb)


# --------------------------------------------------------------------------- #
# 桥
# --------------------------------------------------------------------------- #
class WhiteBalanceBridge:
    """appsink →（numpy 校正）→ appsrc。

    线程是**每个会话一个**（每路观看者一条 pipeline），各自校准各自的。
    自校准只在前 WB_CALIB_FRAMES 帧做，之后增益就冻住了 —— 不做持续跟踪，
    避免画面里出现大片单色时颜色跟着漂。
    """

    def __init__(self, sink, src, spec):
        self.sink = sink
        self.src = src
        self.auto, fixed = parse_spec(spec)
        self.kr, self.kb = fixed or WB_DEFAULT_GAINS
        self.matrix = None if self.auto else matrix_for(self.kr, self.kb)
        self._means = [0.0, 0.0, 0.0]
        self._clip = 0.0
        self._acc_n = 0
        self._pulled = 0
        self._idle = 0
        self._stop = threading.Event()
        self._thread = None

    # ---------------- 生命周期 ----------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="camera-wb", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        """收线程。**调用前先把 pipeline 设成 NULL** —— 那样 appsrc 被 flush，
        卡在满队列上的 push 会立刻返回，不用等超时。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def describe(self):
        if self.matrix is None:
            return "白平衡 自校准中（前 %d 帧）" % WB_CALIB_FRAMES
        return "白平衡 R×%.2f B×%.2f%s" % (
            self.kr, self.kb, "（自校准）" if self.auto else "")

    # ---------------- 干活 ----------------
    def _run(self):
        while not self._stop.is_set():
            sample = self.sink.try_pull_sample(WB_POLL_NS)
            if sample is None:
                # appsink 的 is_eos() 在 pipeline 进 PAUSED **之前**就是 True（实测），
                # 所以必须拉到过数据之后才信它，否则线程一上来就自己收工了。
                if self._pulled and self.sink.is_eos():
                    self.src.end_of_stream()
                    return
                self._idle += 1
                if self._idle * WB_POLL_NS > WB_IDLE_GIVEUP_NS:
                    log.warning("白平衡桥：%d 秒没等到帧，线程收工"
                                "（pipeline 照常跑，只是不再校正）",
                                WB_IDLE_GIVEUP_NS // (1000 * 1000 * 1000))
                    return
                continue
            self._idle = 0
            self._pulled += 1
            if not self._push(sample):
                return

    def _push(self, sample):
        Gst = require_gst()
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        out = self._convert(data)
        if out is not data:
            # 换了内容：新 buffer 要自己带上时间戳，否则 webrtcbin 那边没了时钟
            # 基准（appsrc 是 do-timestamp=false，它不会替我们补）。
            nb = Gst.Buffer.new_wrapped(out)
            nb.pts, nb.dts, nb.duration = buf.pts, buf.dts, buf.duration
        else:
            nb = buf
        ret = self.src.push_buffer(nb)
        if ret == Gst.FlowReturn.OK:
            return True
        if ret == Gst.FlowReturn.FLUSHING:
            return False          # 正常收工（pipeline 已经在往 NULL 走）
        log.warning("白平衡桥推帧失败: %s", ret)
        return False

    def _convert(self, data):
        """返回校正后的 bytes；还没校准时**原样返回同一个对象**（零拷贝直通）。"""
        if self.matrix is None:
            if self._acc_n < WB_CALIB_FRAMES:
                self._accumulate(data)
                return data
            self._freeze()
            if self.matrix is None:
                return data       # 校准失败，已经按默认增益设好了
        return correct_uyvy(data, self.matrix)

    def _accumulate(self, data):
        np = require_numpy()
        u8 = np.frombuffer(data, np.uint8).reshape(-1, 4)
        self._means[0] += float(u8[:, 1].mean() + u8[:, 3].mean()) * 0.5
        self._means[1] += float(u8[:, 0].mean()) - 128.0
        self._means[2] += float(u8[:, 2].mean()) - 128.0
        self._clip += float(((u8[:, 1] > 250) | (u8[:, 3] > 250)).mean())
        self._acc_n += 1

    def _freeze(self):
        """自校准到点：算增益、定矩阵、报一行账。"""
        n = max(self._acc_n, 1)
        mean_y, mean_cb, mean_cr = (x / n for x in self._means)
        clip = self._clip / n
        gains, (r, g, b), why = gray_world_gains(mean_y, mean_cb, mean_cr, clip)
        if gains is None:
            log.warning(
                "白平衡自校准：前 %d 帧折算出的 R/G/B 均值是 %.1f/%.1f/%.1f、"
                "过曝像素 %.1f%%，%s，算不出可靠的增益，改用默认 R×%.2f B×%.2f",
                n, r, g, b, clip * 100, why, *WB_DEFAULT_GAINS)
            if why.startswith("画面过曝"):
                log.warning("  过曝的像素已经丢了颜色，均值把三通道**拉平**，算出来的"
                            "增益偏小。把摄像头增益调低再重开一次")
            gains = WB_DEFAULT_GAINS
        self.kr, self.kb = gains
        self.matrix = matrix_for(*gains)
        log.info("白平衡自校准（前 %d 帧，灰世界）：均值 R=%.1f G=%.1f B=%.1f、"
                 "过曝 %.1f%% → R×%.3f B×%.3f，之后冻结",
                 n, r, g, b, clip * 100, self.kr, self.kb)

        # 灰世界假设"这 n 帧平均下来是灰的"，所以场景本身的颜色会被一起补掉：
        # 对着有色表面校准，算出来的就偏。和默认值差一大截就提醒一句。
        drift = max(abs(self.kr - WB_DEFAULT_GAINS[0]) / WB_DEFAULT_GAINS[0],
                    abs(self.kb - WB_DEFAULT_GAINS[1]) / WB_DEFAULT_GAINS[1])
        if drift > 0.25:
            log.warning("白平衡自校准结果（R×%.2f B×%.2f）和默认值（R×%.2f B×%.2f）"
                        "差了 %.0f%% —— 灰世界把这 %d 帧的平均色当成「本该是灰的」，"
                        "所以启动时镜头对着的若是大片有色表面，那个颜色会被一起补掉。"
                        "找个白墙重开一次，或者直接固定成 --camera-wb %.2f,%.2f",
                        self.kr, self.kb, *WB_DEFAULT_GAINS, drift * 100, n,
                        *WB_DEFAULT_GAINS)
