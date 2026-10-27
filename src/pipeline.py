#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py —— GStreamer 管线描述与搭建

这条管线做一件事：把板子上的摄像头/麦克风变成一条 WebRTC 流。

    摄像头 ──> mpph264enc ──> rtph264pay ──┐
                                          ├──> webrtcbin ──> 浏览器
    麦克风 ──> opusenc    ──> rtpopuspay ──┘

反方向（浏览器 → 板子扬声器）是**运行期**才会接上的：webrtcbin 的 src pad
要等远端描述设好才出现，所以那条链不能用描述串写死，见 attach_audio_return()。

管线被拆成两半，是为了支持**运行期换摄像头**（见 VideoSwitch）：

    骨架（建一次，永不动）        可替换的采集段（每次切换整段换掉）
    webrtcbin ← vq ← rtph264pay ← v4l2src ! [mppjpegdec] ! mpph264enc ! h264parse
      ↑ 音频 aq ← rtpopuspay

切分点选在 h264parse 之后，因为编码输出是两路唯一必须一致的东西 ——
一致就不用重新协商，这是"瞬时切换"的全部依据。

textoverlay（阶段三）在 segment_desc 里加。

**本模块必须在 Gst.init() 之前被导入**，理由见下面那段环境变量处理。
"""

import os

import sources
import whitebalance

# --------------------------------------------------------------------------- #
# 先拆掉板子 BSP 埋的雷（必须早于 Gst.init()，所以放在 import gi 之前）
# --------------------------------------------------------------------------- #
# Orange Pi 5 的 /etc/profile.d/gst.sh 里有 `export GST_V4L2_USE_LIBV4L2=1`。
# 那个文件**只被登录 shell 加载**，于是：
#     ssh 板子 'python3 device_agent.py'   → 变量不存在，正常
#     登进去手动跑同一条命令               → 变量存在，rkisp 分配缓冲失败
# 报错长这样，且和摄像头本身、USB 带宽、"设备掉线"全都没关系：
#     Failed to allocate required memory. | 元素=v4l2src0
#     gstv4l2src.c(759): gst_v4l2src_decide_allocation (): Buffer pool activation failed
#
# 原因是 libv4l2 会走 /usr/lib/aarch64-linux-gnu/libv4l/plugins/libv4l-mplane.so，
# 它把 rkisp 的**多平面**节点按单平面去模拟，VIDIOC_REQBUFS 必然失败。
#
# 注意：设成 '0' **关不掉** —— GStreamer 只判断这个变量非空。只能 unset。
# 板子上实测：同一个管线带它必挂、不带它连跑 20 次全过。
#
# 必须发生在 Gst.init() 之前：v4l2 插件是在创建 element 时读这个变量的。
_LIBV4L2_ENV = "GST_V4L2_USE_LIBV4L2"
LIBV4L2_WAS_FORCED = os.environ.pop(_LIBV4L2_ENV, None) is not None

# 视频分支里用的原始格式现在归 src/sources.py 管（每个源一个 fourcc）：
# MIPI ISP 的原生输出就是 UYVY，USB 摄像头出 MJPG。两路都直接喂 mpph264enc，
# 中间一个 videoconvert 都不需要（实测确认过）。

# RTP 载荷类型。选 102 而不是常见的 96，是为了避开同时抓包时和别的流看混。
PT_VIDEO = 102

# 音频载荷类型。111 是 Opus 在 WebRTC 里的事实标准（Chrome/Firefox 的 offer
# 里默认就有这一项），这里跟着用，别自己挑一个没协商的号。
PT_AUDIO = 111

# Opus 内部就工作在 48kHz。写别的值也行（webrtcbin 会重采样），但没必要：
# 采集/播放两端都支持 48k，中间一次重采样都不要。
AUDIO_RATE = 48000

# 人像检测旁路（阶段四）。检测只需要看，不需要高帧率：
#   · 5fps 下 NPU 单帧 ~30ms，只占单核约 15%；
#   · 帧小一点（640x360）缩放/转换便宜，也足够 YOLOv5 的 640 输入；
#   · 归一化坐标和分辨率解耦，页面按画布尺寸画框。
# 尺寸必须和 detect.py 的稳定输出保持一致 —— 换这里也要确认检测链路仍能协商。
DETECT_W = 640
DETECT_H = 360
DETECT_FPS_DEFAULT = 5


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def request_pad(element, name):
    """GStreamer >= 1.20 用 request_pad_simple，更老的版本只有 get_request_pad。"""
    if hasattr(element, "request_pad_simple"):
        return element.request_pad_simple(name)
    return element.get_request_pad(name)


def guess_local_ip():
    """取默认路由对应的本机 IP。

    连的是 8.8.8.8 但**不会真的发包** —— UDP connect 只是让内核选一条路由，
    然后把选中的源地址告诉我们。失败就退回 127.0.0.1（比如完全没网时）。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# 分支描述
# --------------------------------------------------------------------------- #
def raw_caps(a, src):
    """摄像头原生 raw 格式的 caps。桥的两头都要写死同一份，别各写一遍。

    宽高帧率取自 args 而**不是**取自这个源：两路必须出同一份 caps，
    理由见 sources.py 开头那段（切换不用重新协商就依赖这一点）。
    """
    return ("video/x-raw,format={fmt},width={w},height={h},framerate={fps}/1"
            .format(fmt=src.fourcc, w=a.width, h=a.height, fps=a.fps))


def input_caps(a, src):
    """在 v4l2src 上要求摄像头出什么格式。

    MIPI 直接出原始 YUV；USB 摄像头出 MJPG —— 后者在 USB2 上便宜得多，
    代价是多一个解码器（mppjpegdec 是硬解，出 NV12，mpph264enc 直接吃）。
    """
    if src.kind == "mjpeg":
        return ("image/jpeg,width={w},height={h},framerate={fps}/1"
                .format(w=a.width, h=a.height, fps=a.fps))
    return raw_caps(a, src)


def video_tail(a):
    """视频分支的**尾段**：编码好的 H.264 → RTP → 队列。这部分常驻骨架。

    **`rtph264pay` 不能跟着采集段一起换。** RTP 的时间戳基准（90kHz 映射 +
    随机初值）和 SSRC 都由它维持；留在骨架里，切换摄像头时它们不会重来一遍，
    浏览器就不会把新画面当成一路新流、把 jitterbuffer 清空重来。
    会打乱 PTS 的只有重建**顶层** pipeline（那会拿到新的 base time），
    而换采集段恰好避开了这一点 —— 所以切换不需要任何时间戳补偿代码。
    """
    return (
        # config-interval=-1 = 每个 IDR 帧都重发一次 SPS/PPS。
        # 这个属性在 rtph264pay 上（默认是 0=完全关闭），**不是在 h264parse 上**。
        # 忘了设的症状：第一个观看者正常，之后进来的一律黑屏。
        "rtph264pay name=vpay pt={pt} mtu=1200 config-interval=-1 ! "
        "application/x-rtp,media=video,encoding-name=H264,payload={pt},"
        "clock-rate=90000 ! "
        # 绝不能写成 max-size-buffers=1 leaky=downstream。
        # RTP 是逐包推送的，队列只留 1 个 buffer，webrtcbin 稍慢一点就丢包，
        # 接收端的症状是"收到 RTP 包但 depay 解不出任何一帧"。
        "queue name=vq max-size-buffers=200 max-size-bytes=0 max-size-time=0"
    ).format(pt=PT_VIDEO)


def _detect_fps(a):
    """检测旁路的采样率，夹在 1~15。

    上限 15 不是因为 NPU 跑不动（30 也跑得动），而是这条旁路的价值随帧率
    边际递减：监控场景里 5fps 已经够看清人，再高只是白烧 CPU/NPU。
    传 0 或负数会让 videorate 的 caps 非法，所以这里统一夹住。
    """
    try:
        fps = int(getattr(a, "detect_fps", DETECT_FPS_DEFAULT))
    except (TypeError, ValueError):
        fps = DETECT_FPS_DEFAULT
    return max(1, min(15, fps))


def segment_desc(a, src):
    """可替换的**采集段**：v4l2src → 硬件 H.264 → h264parse。

    终端元素显式命名成 `vtail`：VideoSegment 靠它找 ghost pad 的目标。
    不命名就只能靠 parse 自动生成的 `h264parse0/1` 去猜 —— 换一次源就多一个
    编号，那是个没必要留的坑。

    `--camera-wb` 时（且只对 raw 源），摄像头和编码器之间会被**拆成两条互不
    相连的 chain**，中间由 whitebalance.WhiteBalanceBridge 的线程接起来。
    拆的是描述串，和别的元素没关系 —— 除了多一个 appsink/appsrc，链路对上下
    两端完全一样。USB 那路是 MJPG，桥按 4 字节 UYVY 解包（correct_uyvy），
    对它没有意义，所以整条跳过。
    """
    head = "v4l2src name=vsrc0 device={dev} ! {caps} ! ".format(
        dev=src.device, caps=input_caps(a, src))
    if src.kind == "mjpeg":
        head += "mppjpegdec ! "

    tail = (
        # 遥操作叠加层。放在 tail 的**最前面**，mpph264enc 之前 ——
        # 这样开不开 --camera-wb 都经过同一个元素（wb 那条路是
        # appsrc ! caps ! 这里 ! mpph264enc）。
        #
        # wait-text 默认是 **true**：不显式关掉的话，在第一次 set text 之前
        # 一帧都不往下游推 —— 症状是"加了 textoverlay 之后画面全黑"。
        # silent=false 才是真的渲染；shaded-background 让人在亮画面上也读得清。
        # 文字只用 ASCII：板子上没有中文字体（fc-list :lang=zh 为 0），
        # 中文会变成一排方框。
        # textoverlay 的 sink caps 同时吃 UYVY 和 NV12，所以 mipi 和 usb
        # 两路都不需要 videoconvert。
        "textoverlay name=teleop wait-text=false silent=false auto-resize=false "
        "font-desc=\"DejaVu Sans 20\" halignment=left valignment=top "
        "shaded-background=true deltax=16 deltay=12 ! "
        #
        # 人像检测旁路（阶段四）在这里分叉：主路照旧编码，旁路降采样给
        # Python 检测线程。放在 textoverlay **之后**是刻意的 —— 检测看到的是
        # 浏览器最终看到的画面；那点角落文字不影响人体检测。
        #
        # tee 的每个分支都必须跟一个 queue，否则一路慢会拖住另一路。
        # 主路放在前面、带自己的 queue，保证编码器不被检测旁路卡住。
        "tee name=vtap "
        "vtap. ! queue ! "
        #
        # profile/level 必须显式写。mpph264enc 默认给的是 High profile
        # (profile-level-id=640028)，浏览器多半直接拒收；写成 baseline+31
        # 得到 42c01f（Constrained Baseline 3.1），Chrome/Firefox 都认。
        #
        # 别想着改用 capsfilter `video/x-h264,profile=baseline` 去设 ——
        # 那样反而会把整个 fmtp 行去掉，浏览器连 profile 都读不到，更糟。
        #
        # gop：关键帧间隔。这个值不能省，因为 config-interval=-1 只在**确实有
        # 关键帧**时才有用，否则中途加入的浏览器永远等不到 SPS/PPS，表现是一直黑屏。
        "mpph264enc profile=baseline level=31 gop={gop} bps={bps} rc-mode=cbr ! "
        "h264parse name=vtail "
        #
        # 检测旁路。valve 默认关（drop=true）：不开检测时，帧到这里就被丢掉，
        # 后面的 videorate/videoscale/videoconvert 完全不工作，CPU 开销接近 0。
        # 页面点开关时 session 只改这个 valve 的 drop 属性，不需要重建采集段。
        #
        # 顺序不能换：**先 videorate 再缩放/转换**。videorate 在 30fps 入口上
        # 便宜地丢到 5fps，后面几步只处理 5fps；反过来会按 30fps 做全量转换。
        #
        # videoscale 不能省：videoconvert **不做缩放**，直接写
        # `videoconvert ! video/x-raw,width=640` 会在链路上 not-negotiated。
        # （板子上实测过，症状是 v4l2src 报 Internal data stream error，
        #  和摄像头本身没关系。）
        "vtap. ! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 "
        "leaky=downstream ! "
        "valve name=dvalve drop=true ! "
        "videorate ! video/x-raw,framerate={dfps}/1 ! "
        "videoscale ! video/x-raw,width={dw},height={dh} ! "
        "videoconvert ! video/x-raw,format=RGB ! "
        #
        # async=false **不能省**，理由和上面那些不是一个量级的问题：
        # 换源时新段是单独 sync_state_with_parent() 接进已 PLAYING 的管线的，
        # 这时 bin 的状态迁移会退化成"每个 sink 各自 preroll 完才算到 PLAYING"。
        # 而 detsink 前面那个 valve 默认是关的（drop=true），它**永远收不到
        # 第一帧**，于是异步迁移永远挂在那里、整个 bin 停在 PAUSED。mjpeg 源
        # 的主路不经过任何 appsink，照样出帧，看不出问题；raw 源（mipi）的主路
        # 是 wbsink →（桥线程）→ wbsrc，wbsink 也停在 PAUSED 上不消费，
        # 症状就只有一句"白平衡桥：5 秒没等到帧"，视频 0 包、检测 0 帧。
        # 实测：GST_STATES 里 bin 的 PAUSED→PLAYING 返回 ASYNC、detsink 一直
        # "changing state asynchronously"；同一段给 detsink 加 async=false，
        # 桥立刻开始出帧（前 15 帧自校准都跑完了）。
        # 检测本来就不需要 preroll（frame 到了就拉，没有就等），关掉只有好处。
        "appsink name=detsink max-buffers=1 drop=true sync=false async=false"
    ).format(gop=a.gop, bps=a.bitrate,
             dfps=_detect_fps(a), dw=DETECT_W, dh=DETECT_H)

    if src.kind != "raw" or not getattr(a, "camera_wb", None):
        return head + tail

    # 管线本来就是 UYVY，桥也工作在 UYVY 域 —— 所以这里**一次 videoconvert 都不需要**，
    # 摄像头出来的字节原样进 appsink、校正完原样进编码器。
    return (head
            + whitebalance.WB_SINK_DESC
            + whitebalance.WB_SRC_DESC.format(caps=raw_caps(a, src))
            + tail)


def skeleton_desc(a):
    """常驻骨架：webrtcbin + 视频尾段 +（可选）音频分支。

    建一次就不再动。切换摄像头时被整段替换掉的只有采集段，
    所以挂在这上面的东西（vpay 的统计探针、vq→sink_0、音频、回程链）
    全都不受影响。
    """
    desc = "webrtcbin name=wbr bundle-policy=max-bundle " + video_tail(a)
    if audio_enabled(a):
        desc += " " + audio_branch(a)
    return desc


# --------------------------------------------------------------------------- #
# 搭建
# --------------------------------------------------------------------------- #
def audio_enabled(a):
    """音频分支要不要建。

    --no-audio，或者启动时没探到麦克风（audio.resolve 已经报过错），就不建。
    """
    return not getattr(a, "no_audio", False) and bool(getattr(a, "mic_device", None))


def audio_branch(a):
    """音频分支：麦克风 → Opus → RTP。

    **不要在这条链上写死 alsasrc 的声道数。** 板子上两张卡的采集能力是反的：
    ES8388（3.5mm）只支持立体声，USB 摄像头只支持单声道，写死哪一个都会让另一
    张卡直接协商失败（报 "Channels count not available"）。让 alsasrc 用它自己
    的格式出来，downmix 交给 audioconvert。

    末尾 queue 的理由和视频那路一样，见 video_tail 的注释。
    """
    return (
        "alsasrc device={mic} ! "
        "audioconvert ! audioresample ! "
        # 到这儿才定成单声道：Opus 这一路只需要单声道，
        # 前端是立体声（ES8388）时由 audioconvert 混合下来。
        "audio/x-raw,format=S16LE,rate={rate},channels=1 ! "
        "opusenc bitrate={bps} ! "
        "rtpopuspay name=apay pt={pt} ! "
        "application/x-rtp,media=audio,encoding-name=OPUS,payload={pt},"
        "clock-rate={rate} ! "
        "queue name=aq max-size-buffers=200 max-size-bytes=0 max-size-time=0"
    ).format(mic=a.mic_device, rate=AUDIO_RATE, pt=PT_AUDIO, bps=a.audio_bitrate)


def _build_return_chain(pipeline, a):
    """建 浏览器 → 扬声器 那条链，返回元素列表（第一个是 queue）。

    逐个 ElementFactory.make 再 add，而不是 parse_bin_from_description：
    这样元素名是**流水线级**的，统计探针可以直接 pipeline.get_by_name("adec")
    拿到，不用在 bin 里绕。
    """
    from gi.repository import Gst

    spec = [
        # queue 必须在最前面：它把 webrtcbin 的 streaming 线程和 alsasink
        # 解耦开。少了它，扬声器一卡就会反压到 webrtcbin 的接收路径。
        ("rq", "queue", {"max-size-buffers": 200, "max-size-bytes": 0,
                         "max-size-time": 0}),
        ("adepay", "rtpopusdepay", {}),
        ("adec", "opusdec", {}),
        ("aconv", "audioconvert", {}),
        ("aresamp", "audioresample", {}),
        ("asink", "alsasink", {"device": a.spk_device}),
    ]

    els = []
    for name, factory, props in spec:
        el = Gst.ElementFactory.make(factory, name)
        if el is None:
            raise RuntimeError("建不出元素 %s（%s）" % (name, factory))
        for key, value in props.items():
            el.set_property(key, value)
        pipeline.add(el)
        els.append(el)

    for prev, nxt in zip(els, els[1:]):
        if not prev.link(nxt):
            raise RuntimeError("回程链 %s → %s 连不上"
                               % (prev.get_name(), nxt.get_name()))

    # 流水线此刻已经是 PLAYING，新加进来的元素默认停在 NULL，
    # 每个都要单独同步上去，否则无声且不报错。
    for el in els:
        el.sync_state_with_parent()
    return els


def attach_audio_return(webrtcbin, pipeline, a, log, on_elements=None):
    """浏览器 → 板子：把 webrtcbin 出来的音频 RTP 接到扬声器。

    这条链只能运行期建：webrtcbin 的 src pad 要等 set-remote-description
    之后才出现（而且**远端真开始发**才会有数据）。两个坑都在下面：

    1. `pad-added` 对**每一个**新建的 src pad 都触发，视频那路也会进来。
       必须按 caps 分辨；"来了就接"会把视频的 RTP 喂给 rtpopusdepay。
    2. `pad-added` 时 caps 有可能是 **NULL**（pad 刚建出来、还没协商）。
       这时候直接 return 会把整路音频**静默丢掉** —— 日志干干净净，
       没有一条报错，浏览器那边也显示已连接，就是没声音。
       所以 caps 为空时挂一个一次性探针，等第一帧到了拿到 caps 再决定。

    **还有一个前置条件，不在本函数里，但排查时必须先想到它**：这个回调
    压根不会触发，除非**对端的 SDP 里带 `a=ssrc:<数字> cname:<非空>`**。

    webrtcbin 在 `on_rtpbin_pad_added` 里是靠 `stream->remote_ssrcmap`
    把进来的 RTP 认到某条 transceiver 上的，而那张表**只在
    `_set_description_task` 解析远端 SDP 时填**（1.20.3 源码 5806-5820 行），
    认出条目要求：属性名是 `ssrc`，值的第一段是数字，**第二段以 `cname:` 开头**。
    匹配不上就只剩一条退路：拿 rtpbin 的 session id 当 m-line 下标去找
    transceiver —— 而 max-bundle 下所有 m-line 共用 session 0，于是**永远
    找到第 0 条（视频）**，它没有 src pad，`gst_ghost_pad_set_target(NULL)`
    一连串断言，`pad-added` 永远不会发出来。

    症状因此长得非常像"板子有 bug"：ICE connected、视频好好的、板子日志里
    连一行中文报错都没有，就是 `audio_frames_decoded` 一直是 0。
    但根因在对端。**Chrome / Firefox 的 answer 都带 `a=ssrc:.. cname:..`**，
    所以真实浏览器不受影响；会踩到的是自己拿 webrtcbin 写的对端 ——
    GStreamer 当 answerer 时**不吐这一行**（它当 offerer 时吐）。
    照着 `tools/selftest.py` 里 `_announce_ssrc()` 补上即可。

    另外：pad-added 本身跑在 GLib 线程（可以放心建元素），但**探针回调跑在
    streaming 线程**上，所以探针里只能读 caps，真正的建链要用 GLib.idle_add
    丢回主线程做。
    """
    from gi.repository import GLib, Gst

    state = {"done": False}

    def is_audio(caps_text):
        # caps 形如 `application/x-rtp, media=(string)audio, payload=(int)111, ...`
        # 空格的位置不固定，先统一去掉再比。
        t = (caps_text or "").replace(" ", "")
        return "application/x-rtp" in t and "media=(string)audio" in t

    def connect(pad, caps_text):
        if state["done"]:
            return
        state["done"] = True
        try:
            els = _build_return_chain(pipeline, a)
        except Exception:  # noqa: BLE001
            log.exception("建音频回程链失败 —— 浏览器将听不到板子的声音")
            return
        # 从 webrtcbin 的 src pad 连到回程链的 queue。
        res = pad.link(els[0].get_static_pad("sink"))
        if res != Gst.PadLinkReturn.OK:
            log.error("音频回程连接失败: %s（浏览器听不到板子）", res)
            return
        log.info("音频回程已接上: webrtcbin → alsasink device=%s（%s）",
                 a.spk_device, caps_text.split(",")[1].strip() if "," in caps_text else "")
        if on_elements:
            on_elements({el.get_name(): el for el in els})

    def on_probe(pad, info):
        caps = pad.get_current_caps()
        text = caps.to_string() if caps else ""
        if not text:
            return Gst.PadProbeReturn.OK
        # 探针是对**所有** caps 为 NULL 的 src pad 挂的，视频那一路也会走到
        # 这里 —— 所以必须再判一次是不是音频。漏了这个判断，视频 pad 会抢先
        # 把回程链建走（connect 里的 state["done"] 一置位就再也回不来），
        # 真正的音频永远接不上，而日志上一切正常、一条报错都没有。
        pad.remove_probe(info.id)
        if is_audio(text):
            GLib.idle_add(connect, pad, text)
        else:
            log.debug("探针拿到的是非音频 caps，忽略: %s", text.split(",")[0])
        return Gst.PadProbeReturn.OK

    def on_pad_added(_element, pad):
        # **只认 src pad。** `pad-added` 对**任何**新加进来的 pad 都触发，
        # 包括我们自己 request_pad 出来的 sink 0/1。当前 build() 是先 _link
        # 出 sink_0/sink_1、再调用本函数，所以那两个 pad 早就存在、不会再触发 ——
        # 但这个顺序一旦被改动（比如以后挪到协商之后再 request_pad），
        # sink_1 就会先到这里，且 caps 里正好写着 media=audio，
        # 于是它把这一路占了，真正的音频 src pad 反而被丢掉：
        # 症状是浏览器一个包都收不到，而日志上写着"音频回程已接上"。
        # 加这一行不花什么代价，把这个顺序依赖去掉。
        if pad.get_direction() != Gst.PadDirection.SRC:
            log.debug("pad-added 是 %s（非 src），忽略", pad.get_name())
            return

        caps = pad.get_current_caps()
        text = caps.to_string() if caps else ""
        if not text:
            log.debug("pad-added 时 caps 还是 NULL，挂一次性探针等第一帧")
            # BUFFER|BUFFER_LIST 两个都要接：RTP 推送有可能整批走
            # gst_pad_push_list()，只接 BUFFER 的探针会一次都不触发，
            # 于是这路音频**永远不会被接上**，而且安静得没有一条日志
            # （同一个坑在 session.py 的统计探针上已经踩过一次）。
            pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                          on_probe)
            return
        if is_audio(text):
            connect(pad, text)
        else:
            # 视频那路也会走到这里。它不是错误，打个 debug 就够了。
            log.debug("pad-added 不是音频，忽略: %s", text.split(",")[0])

    webrtcbin.connect("pad-added", on_pad_added)


def apply_ice(webrtcbin, ice, log):
    """把 STUN/TURN 配置写进 webrtcbin。

    **必须在 request_pad 之前做完**，顺序别动。
    """
    ice = ice or {}
    if ice.get("stun"):
        webrtcbin.set_property("stun-server", ice["stun"])
        log.info("STUN: %s", ice["stun"])
    if ice.get("turn"):
        # 这个 URI 由信令服务器生成，user/pass 已经 percent-encode 过了。
        # 千万不要在这里自己拼字符串：webrtcbin 解析 turn:// 时**只按第一个 ':'**
        # 切分 userinfo，而 TURN REST 的用户名天生就含一个 ':'（形如 "<过期时间>:<房间>"），
        # 不转义会被切成 user="1758600000"、pass="demo:xxxx"，认证必然 401，
        # 而且只在 GST_DEBUG=webrtcice:5 里留一行很容易忽略的 WARNING。
        webrtcbin.set_property("turn-server", ice["turn"])
        log.info("TURN: %s", ice["turn"])
    if (ice.get("policy") or "all") != "all":
        # relay → libnice 的 force-relay：拿到中继地址之前不产生任何本地候选。
        # 配错时的症状是 **offer 里一条 a=candidate 都没有**，而不是悄悄退回 host。
        webrtcbin.set_property("ice-transport-policy", ice["policy"])
        log.info("ICE 传输策略 = %s（强制中继）", ice["policy"])
    if not ice.get("stun") and not ice.get("turn"):
        log.info("未配置 STUN/TURN，只用 host candidate（同网段可用）")


def confirm_null(el, what, log):
    """元素（或 bin）从 NULL 回来之后，确认它**真的**到了 NULL；没到就把卡住的点名。

    为什么值得专门写一段：设备 fd 是在采集元素的 PAUSED→READY 里还回去的，
    所以"状态没走到 NULL"和"摄像头一直被占着"是同一件事的两种说法。而这件事
    **不会**通过别的渠道暴露 —— 日志上没有错误、会话照常关掉、下一轮还能看，
    唯有 `fuser /dev/video11`（或几十轮之后 fd 耗尽）能发现。

    实测（板子上，2026-10-14）：一个**不含本项目代码**的裸 pipeline
    `v4l2src device=/dev/video11 ! ... ! fakesink`，PLAYING 时这个设备上挂着
    **2 个** fd（v4l2src 自己开两次，flags 都是 0x400002），进 NULL 之后
    **2 个全还**。所以"运行中 2 个"是正常的，剩 1 个才是问题 —— 而且问题一定
    出在这条链上某个元素没走完 NULL，不在"谁没析构"。
    """
    from gi.repository import Gst

    ret, state, pending = el.get_state(0)
    if state == Gst.State.NULL and pending == Gst.State.VOID_PENDING:
        log.debug("%s 已到 NULL", what)
        return True

    log.warning("%s 没到 NULL（%s，当前 %s / 待 %s），卡住的元素：",
                what, ret.value_nick, state.value_nick, pending.value_nick)
    try:
        it = el.iterate_recurse()
        while True:
            ok, child = it.next()
            if ok != Gst.IteratorResult.OK:
                break
            cret, cstate, _cp = child.get_state(0)
            if cstate != Gst.State.NULL:
                fac = child.get_factory()
                log.warning("  %s（%s）→ %s / 当前 %s",
                            child.get_name(),
                            fac.get_name() if fac else "?",
                            cret.value_nick, cstate.value_nick)
    except Exception:                                # noqa: BLE001
        # 这段只是诊断，绝不能让"报不出来"变成"拆会话失败"。
        log.exception("列卡住的元素时出错（不影响拆会话）")
    return False


class VideoSegment:
    """一段采集链：v4l2src → [解码器] → 编码器 → h264parse(name=vtail)。

    整段随时可以被换掉，这就是"切换摄像头不用重新协商"的来源 ——
    骨架里的 webrtcbin / rtph264pay / 音频分支一个都不动。

    只由 VideoSwitch 创建和销毁，**只能在 GLib 线程碰**。
    """

    def __init__(self, a, src, log):
        from gi.repository import Gst

        if src.kind == "raw" and getattr(a, "camera_wb", None):
            # 必须早于 parse：GstApp 没 import 的话，PyGObject 会按 GObject 类型
            # 缓存包装对象，get_by_name("wbsink") 拿到的是个没有 try_pull_sample
            # 的秃 Element，之后再 import 也换不回来。见 whitebalance.prepare()。
            whitebalance.prepare()

        desc = segment_desc(a, src)
        self.bin = Gst.parse_bin_from_description(desc, False)
        if self.bin is None:
            raise RuntimeError("采集段建不出来（源 %s）：%s" % (src.name, desc))
        self.src = src

        vtail = self.bin.get_by_name("vtail")
        if vtail is None:
            raise RuntimeError("采集段里没有 vtail（描述串写错了？）：%s" % desc)

        # 遥操作叠加层。它和采集段同生共死：换摄像头时整段重建，session 必须
        # 重新拿一次新段里的这个元素（见 DeviceSession._bind_overlay）。
        self.overlay = self.bin.get_by_name("teleop")
        if self.overlay is None:
            raise RuntimeError("采集段里没有 teleop 叠加层（描述串写错了？）：%s" % desc)

        # 检测旁路的两个把手：valve 控制开关，appsink 是检测线程的输入。
        # 它们同样随采集段重建，session 换源后要重新绑定（_bind_detect）。
        #
        # **调到这里之前 GstApp 必须已经 import**（detect.prepare_appsink），
        # 否则 get_by_name 拿到的 detsink 是个没有 try_pull_sample 的秃 Element。
        self.dvalve = self.bin.get_by_name("dvalve")
        if self.dvalve is None:
            raise RuntimeError("采集段里没有 dvalve（描述串写错了？）：%s" % desc)
        self.detsink = self.bin.get_by_name("detsink")
        if self.detsink is None:
            raise RuntimeError("采集段里没有 detsink（描述串写错了？）：%s" % desc)

        # ghost 必须在**还没接进骨架**的时候建：Gst.GhostPad.new() 断言目标 pad
        # 未连接（"assertion '!gst_pad_is_linked (target)' failed"，实测）。
        # 所以顺序永远是 先 parse 出独立 bin → 再 ghost → 最后才 link 到 vpay。
        self.pad = Gst.GhostPad.new("src", vtail.get_static_pad("src"))
        if self.pad is None:
            raise RuntimeError("ghost pad 建不出来（目标 pad 已经连上了？）")
        if not self.bin.add_pad(self.pad):
            raise RuntimeError("ghost pad 挂不上采集段")
        # **实测踩过**：new() 出来的 ghost pad 默认是**非激活**的，add_pad()
        # 之后仍然不激活。漏了这一步，link() 返回 WRONG_HIERARCHY ——
        # 不报错、不出数据、日志干干净净，症状就是"切过去一片黑，什么都不说"。
        self.pad.set_active(True)

        # 桥的两头在描述串里是**空开**的（见 segment_desc），接起来的是这个对象。
        self.wb = None
        wsink = self.bin.get_by_name("wbsink")
        if wsink is not None:
            self.wb = whitebalance.WhiteBalanceBridge(
                wsink, self.bin.get_by_name("wbsrc"), a.camera_wb)
            log.info("%s", self.wb.describe())

    def start_bridge(self):
        if self.wb is not None:
            self.wb.start()

    def teardown(self, pipeline, peer_sink, log):
        """把这一段从运行中的管线上摘下来。

        步骤顺序是硬性的，不能图省事重排：

        1. 先 `set_state(NULL)` 并**等它真的到 NULL**。NULL 会把 appsrc flush
           掉，卡在满队列 push_buffer 上的桥线程才会立刻返回。写反了
           （先 stop 再 NULL）join 会一直等到超时，随后 remove() 掉一个线程
           还在摸的 bin —— 那是未定义行为，不是"慢一点"。
        2. 然后才收桥线程。
        3. 最后 unlink + remove。这时设备节点也已经释放了，下一个源才打得开。
        """
        from gi.repository import Gst

        self.bin.set_state(Gst.State.NULL)
        self.bin.get_state(2 * Gst.SECOND)
        confirm_null(self.bin, "拆下来的旧段", log)

        if self.wb is not None:
            self.wb.stop()
            self.wb = None

        self.pad.set_active(False)
        self.pad.unlink(peer_sink)
        pipeline.remove(self.bin)


class VideoSwitch:
    """骨架 + 当前那段采集。**所有方法都只能在 GLib 线程调用。**

    在 PLAYING 的管线里加删元素会碰 GStreamer 的状态机和 streaming 线程，
    从别的线程调是未定义行为 —— 外部（HTTP 控制端点）要用 GLib.idle_add 投进来。
    """

    def __init__(self, pipeline, pay_sink, a, log):
        from gi.repository import Gst

        self._Gst = Gst
        self.pipeline = pipeline
        self.pay_sink = pay_sink     # rtph264pay 的 sink pad，在骨架里，永不变
        self.a = a
        self.log = log
        self.current = None
        self.n_switches = 0

    @property
    def source(self):
        return self.current.src if self.current is not None else None

    @property
    def overlay(self):
        """当前采集段里的 textoverlay 元素。换段之后指向新的那个。"""
        return self.current.overlay if self.current is not None else None

    @property
    def detsink(self):
        """当前采集段里的检测 appsink。换段之后指向新的那个。"""
        return self.current.detsink if self.current is not None else None

    @property
    def dvalve(self):
        """当前采集段里的检测阀门。换段之后指向新的那个。"""
        return self.current.dvalve if self.current is not None else None

    def attach_first(self, src):
        """接上第一个采集段。**不** sync 状态 —— 由 session.start() 的
        set_state(PLAYING) 统一带上去，路径更少，语义只有一种。"""
        Gst = self._Gst
        seg = VideoSegment(self.a, src, self.log)
        self.pipeline.add(seg.bin)
        if seg.pad.link(self.pay_sink) != Gst.PadLinkReturn.OK:
            self.pipeline.remove(seg.bin)
            raise RuntimeError("采集段接不上 rtph264pay（源 %s）" % src.name)
        self.current = seg
        self.log.info("视频源：%s（%s，%s %dx%d@%d）", src.name, src.device,
                      src.fourcc, self.a.width, self.a.height, self.a.fps)

    def switch(self, name):
        """换一路摄像头。返回最终生效的源名。"""
        Gst = self._Gst
        src = sources.resolve(name)
        if self.current is not None and self.current.src.name == src.name:
            return src.name          # 已经在这一路上，不做无谓的重建（画面不会闪）

        # **先造新的再拆旧的。** parse_bin_from_description 不碰硬件，元素缺失
        # 或者描述串写错都会在这里抛出来，此时旧段毫发无损 —— 最便宜的回滚。
        seg = VideoSegment(self.a, src, self.log)

        old = self.current
        if old is not None:
            old.teardown(self.pipeline, self.pay_sink, self.log)

        self.pipeline.add(seg.bin)
        if seg.pad.link(self.pay_sink) != Gst.PadLinkReturn.OK:
            # 新段接不上。**重建**一路旧源装回去，而不是把刚才拆掉的那个 bin
            # 重新 add 回来：teardown() 已经把它 set_state(NULL)、收掉桥线程、
            # 把 ghost pad 置为非激活，原地复活会留下一个"接上了但桥不转了"的段
            # —— 比直接报错更难查。这条路正常永远走不到（两路编码输出 caps
            # 相同，pay_sink 此时也确实是空的），留着是为了不把管线扔在
            # "没有视频源"的状态里。
            self.pipeline.remove(seg.bin)
            self.current = None
            if old is not None:
                try:
                    back = VideoSegment(self.a, old.src, self.log)
                    self.pipeline.add(back.bin)
                    back.pad.link(self.pay_sink)
                    back.bin.sync_state_with_parent()
                    back.start_bridge()
                    self.current = back
                    self.log.warning("新段接不上，已退回 %s", old.src.name)
                except Exception:               # noqa: BLE001
                    self.log.exception("退回 %s 也失败了，现在没有视频源", old.src.name)
            raise RuntimeError("采集段接不上 rtph264pay（源 %s）" % src.name)

        # **先 link 再 sync。** 反过来新的 v4l2src 会在还没有下游的时候开始推，
        # mpph264enc/h264parse 拿 GST_FLOW_NOT_LINKED，这条错误经 bus 冒出来，
        # 表现成"一换源就报 GStreamer 错误"。
        seg.bin.sync_state_with_parent()
        seg.start_bridge()
        self.current = seg
        self.n_switches += 1
        self.log.info("视频源已切到 %s（%s）", src.name, src.device)
        return src.name

    def start_bridges(self):
        """首段的桥线程。要等 pipeline 真的进 PLAYING 才起 —— 理由见 session.py。"""
        if self.current is not None:
            self.current.start_bridge()

    def dispose(self):
        """整个管线要拆掉了。先 NULL（flush 掉 appsrc）再收桥线程，线程不用等
        超时；NULL 之后 `get_state()` 等它真的到 NULL；最后把采集段从管道里摘掉。

        **为什么最后那一步是必须的** —— 这是设备 fd 每轮会话漏一个的真正原因，
        板子上量出来的。中间试错过好几次，把结论和错路都记在下面。

        现象：跑一次会话，`/dev/video11` 上留下一个 fd 不还（0 → 2 → 1，每轮 +1），
        几十轮之后 fd 耗尽，MIPI 传感器一直被占着不放。

        根因分两步量出来：

        1. **fd 是被 appsink 攥着的。** 对照实验（不含本项目代码的最小 pipeline）：
           `v4l2src ! fakesink`、以及 `v4l2src ! appsink` 但**没人拉过 sample**，
           进 NULL 之后设备 fd **全还**；只要**拉过** sample，就剩 1 个。
           那个 fd 属于 v4l2 的 **buffer pool**：只要还有一个 buffer 没析构，
           pool 就析构不了，pool 就攥着设备 fd。而 appsink 拉过 sample 之后会
           一直攥着一个 buffer —— `appsink.run_dispose()` 能立刻把它还回来
           （GStreamer 为此外加一条 CRITICAL：不该绕过父对象析构），
           说明东西就在 appsink 里。

        2. **appsink 析构不了，是因为 pipeline 析构不了。** `gc.get_referrers`
           点名：pipeline 被闭包格子抓着，其中一个是 `attach_audio_return()` 里
           的 `connect()`（它捕获了 `pipeline`），而这个闭包由 C 侧的 signal
           handler / pad probe 抓着。**这条环横跨 C 和 Python，CPython 的 gc
           穿不过去** —— 把引用全丢掉再 `gc.collect()`，探针实测 pipeline 仍然活着。

        于是 pipeline → bin → appsink → buffer → pool → 设备 fd，一整条链谁也松不了
        手。**在拆会话时把采集段从管道里摘掉**就把这条链断在最上面一环：段不再挂在
        管道上、也没有别的引用 → 段析构 → appsink 析构 → buffer 松手 → pool 析构
        → fd 还回去。实测摘之前每轮漏一个、摘之后归零（连续 3 轮 0 个）。

        这也解释了为什么**切换摄像头从来不漏**：那条路本来就走
        `VideoSegment.teardown()`，里面就有 `pipeline.remove(self.bin)`。
        两个出口本来就该走同一条路。

        （试过但**没用**的，都记下来免得下次再走一遍：NULL 之后加 `get_state()`
        等待、摘掉 bus 的 signal watch、appsink 的 `enable-last-sample=false`、
        以及 `pipeline.run_dispose()` —— 它们都不破上面那个环，环在最上面一环。
        `confirm_null()` 留着，它证明过 pipeline 确实干净地到了 NULL，把状态机
        这一支排除掉了；哪天再漏，它会直接点名卡住的元素。）
        """
        self.pipeline.set_state(self._Gst.State.NULL)
        self.pipeline.get_state(2 * self._Gst.SECOND)
        confirm_null(self.pipeline, "整条 pipeline", self.log)
        if self.current is not None:
            # 摘掉采集段 —— fd 能不能还回去就看这一步，理由见上面 docstring。
            # 走 teardown() 而不是重写一遍，是为了和切换那条路永远是同一段代码。
            self.current.teardown(self.pipeline, self.pay_sink, self.log)
            self.current = None


def build(a, ice, log, source, on_audio_elements=None):
    """搭出整条管线，返回 (pipeline, webrtcbin, switch)。

    webrtcbin 的 sink pad 是 **request pad**，parse_launch 连不了，
    所以这里把 webrtcbin 和各分支并列写在描述串里，链接留到下面手动做。

    source 是初始的采集源。骨架建好之后由 VideoSwitch.attach_first() 接上去 ——
    **第一段一定要在建 pipeline 的时候就在**，别留一个"进了 PLAYING 但还没有
    视频生产者"的时间窗。

    on_audio_elements 在**回程链建出来之后**被调用（参数是 {名字: 元素}），
    给 session.py 挂统计探针用。回程链是运行期才建的，所以只能回调，不能返回。
    """
    from gi.repository import Gst

    pipeline = Gst.parse_launch(skeleton_desc(a))
    webrtcbin = pipeline.get_by_name("wbr")
    if webrtcbin is None:
        raise RuntimeError("描述串里没找到 webrtcbin，parse_launch 可能失败了")

    apply_ice(webrtcbin, ice, log)

    # sink 编号决定 m-line 顺序，也就决定浏览器 offer/answer 里的顺序。
    # 视频固定占 sink_0、音频固定占 sink_1，两边永远对得上。
    if not _link(pipeline, webrtcbin, "vq", "sink_0"):
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("视频分支无法连接到 webrtcbin 的 sink_0")

    if audio_enabled(a):
        if not _link(pipeline, webrtcbin, "aq", "sink_1"):
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("音频分支无法连接到 webrtcbin 的 sink_1")
        # 回程（浏览器 → 扬声器）只能等 webrtcbin 的 src pad 出现，
        # 这里只是把回调挂上，真正建链在 attach_audio_return 里。
        attach_audio_return(webrtcbin, pipeline, a, log, on_audio_elements)

    switch = VideoSwitch(pipeline, pipeline.get_by_name("vpay").get_static_pad("sink"),
                         a, log)
    try:
        switch.attach_first(source)
    except Exception:
        # 采集段建不出来（设备不存在 / caps 不支持）时，别把一个已经进到
        # READY 的管线漏在这儿 —— 它会占着设备节点。
        pipeline.set_state(Gst.State.NULL)
        raise

    return pipeline, webrtcbin, switch


def _link(pipeline, webrtcbin, queue_name, pad_name):
    from gi.repository import Gst

    src = pipeline.get_by_name(queue_name).get_static_pad("src")
    sink = request_pad(webrtcbin, pad_name)
    if sink is None:
        return False
    return src.link(sink) == Gst.PadLinkReturn.OK
