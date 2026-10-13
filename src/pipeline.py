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

textoverlay（阶段三）在 video_branch 里加。

**本模块必须在 Gst.init() 之前被导入**，理由见下面那段环境变量处理。
"""

import os

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

# 视频分支里用的原始格式。MIPI ISP 的原生输出就是 UYVY，textoverlay 和
# mpph264enc 都直接吃它，中间一个 videoconvert 都不需要（实测确认过）。
VIDEO_FORMAT = "UYVY"

# RTP 载荷类型。选 102 而不是常见的 96，是为了避开同时抓包时和别的流看混。
PT_VIDEO = 102

# 音频载荷类型。111 是 Opus 在 WebRTC 里的事实标准（Chrome/Firefox 的 offer
# 里默认就有这一项），这里跟着用，别自己挑一个没协商的号。
PT_AUDIO = 111

# Opus 内部就工作在 48kHz。写别的值也行（webrtcbin 会重采样），但没必要：
# 采集/播放两端都支持 48k，中间一次重采样都不要。
AUDIO_RATE = 48000


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
def _raw_caps(a):
    """摄像头原生格式的 caps。桥的两头都要写死同一份，别各写一遍。"""
    return ("video/x-raw,format={fmt},width={w},height={h},framerate={fps}/1"
            .format(fmt=VIDEO_FORMAT, w=a.width, h=a.height, fps=a.fps))


def video_branch(a):
    """视频分支：摄像头 → 硬件 H.264 → RTP。

    末尾那个 queue 是**必须**的，而且它的参数不能乱改，见下面注释。

    `--camera-wb` 时，摄像头和编码器之间会被**拆成两条互不相连的 chain**，
    中间由 whitebalance.WhiteBalanceBridge 的线程接起来。拆的是描述串，
    和别的元素没关系 —— 除了多一个 appsink/appsrc，链路对上下两端完全一样。
    """
    head = "v4l2src device={cam} ! {caps} ! ".format(cam=a.camera, caps=_raw_caps(a))

    tail = (
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
        "h264parse ! "
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
    ).format(gop=a.gop, bps=a.bitrate, pt=PT_VIDEO)

    if not getattr(a, "camera_wb", None):
        return head + tail

    # 管线本来就是 UYVY，桥也工作在 UYVY 域 —— 所以这里**一次 videoconvert 都不需要**，
    # 摄像头出来的字节原样进 appsink、校正完原样进编码器。
    return (head
            + whitebalance.WB_SINK_DESC
            + whitebalance.WB_SRC_DESC.format(caps=_raw_caps(a))
            + tail)


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

    末尾 queue 的理由和视频那路一样，见 video_branch 的注释。
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


def build(a, ice, log, on_audio_elements=None):
    """搭出整条管线，返回 (pipeline, webrtcbin, wb)。

    webrtcbin 的 sink pad 是 **request pad**，parse_launch 连不了，
    所以这里把 webrtcbin 和各分支并列写在描述串里，链接留到下面手动做。

    wb 是白平衡桥对象（没开 --camera-wb 时是 None）。它的线程**不在这里起** ——
    要等 pipeline 进 PLAYING、真的有帧了再起，见 session.py。

    on_audio_elements 在**回程链建出来之后**被调用（参数是 {名字: 元素}），
    给 session.py 挂统计探针用。回程链是运行期才建的，所以只能回调，不能返回。
    """
    from gi.repository import Gst

    if getattr(a, "camera_wb", None):
        # 必须赶在 parse_launch 之前：GstApp 没 import 的话，下面 get_by_name
        # 拿到的 wbsink 是个没有 try_pull_sample 的秃 Element，见 whitebalance。
        whitebalance.prepare()

    # 分支串先各自 format 好再拼。不要写成一整条大字符串最后统一 .format()：
    # 用户给的设备路径里万一带了 '{' / '}'，会被后面的 .format() 当成占位符再解释一遍。
    desc = "webrtcbin name=wbr bundle-policy=max-bundle " + video_branch(a)
    if audio_enabled(a):
        desc += " " + audio_branch(a)

    pipeline = Gst.parse_launch(desc)
    webrtcbin = pipeline.get_by_name("wbr")
    if webrtcbin is None:
        raise RuntimeError("描述串里没找到 webrtcbin，parse_launch 可能失败了")

    apply_ice(webrtcbin, ice, log)

    # 桥的两头在描述串里是**空开**的（见 video_branch），接起来的是这个对象。
    wb = None
    wbsink = pipeline.get_by_name("wbsink")
    if wbsink is not None:
        wb = whitebalance.WhiteBalanceBridge(
            wbsink, pipeline.get_by_name("wbsrc"), a.camera_wb)
        log.info("%s", wb.describe())

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

    return pipeline, webrtcbin, wb


def _link(pipeline, webrtcbin, queue_name, pad_name):
    from gi.repository import Gst

    src = pipeline.get_by_name(queue_name).get_static_pad("src")
    sink = request_pad(webrtcbin, pad_name)
    if sink is None:
        return False
    return src.link(sink) == Gst.PadLinkReturn.OK
