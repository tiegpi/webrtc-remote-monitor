#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py —— GStreamer 管线描述与搭建

这条管线做一件事：把板子上的摄像头变成一条 WebRTC 流。

    摄像头 ──> mpph264enc ──> rtph264pay ──> webrtcbin ──> 浏览器

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


def build(a, ice, log):
    """搭出整条管线，返回 (pipeline, webrtcbin, wb)。

    webrtcbin 的 sink pad 是 **request pad**，parse_launch 连不了，
    所以这里把 webrtcbin 和各分支并列写在描述串里，链接留到下面手动做。

    wb 是白平衡桥对象（没开 --camera-wb 时是 None）。它的线程**不在这里起** ——
    要等 pipeline 进 PLAYING、真的有帧了再起，见 session.py。
    """
    from gi.repository import Gst

    if getattr(a, "camera_wb", None):
        # 必须赶在 parse_launch 之前：GstApp 没 import 的话，下面 get_by_name
        # 拿到的 wbsink 是个没有 try_pull_sample 的秃 Element，见 whitebalance。
        whitebalance.prepare()

    # 分支串先各自 format 好再拼。不要写成一整条大字符串最后统一 .format()：
    # 用户给的设备路径里万一带了 '{' / '}'，会被后面的 .format() 当成占位符再解释一遍。
    desc = "webrtcbin name=wbr bundle-policy=max-bundle " + video_branch(a)

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
    if not _link(pipeline, webrtcbin, "vq", "sink_0"):
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("视频分支无法连接到 webrtcbin 的 sink_0")

    return pipeline, webrtcbin, wb


def _link(pipeline, webrtcbin, queue_name, pad_name):
    from gi.repository import Gst

    src = pipeline.get_by_name(queue_name).get_static_pad("src")
    sink = request_pad(webrtcbin, pad_name)
    if sink is None:
        return False
    return src.link(sink) == Gst.PadLinkReturn.OK
