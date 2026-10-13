#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/selftest.py —— 无头对端：不用浏览器，把双向音频真的跑一遍

它做的事和浏览器完全一样：连信令、收 offer、回 answer、交换候选。区别只有
一个 —— 它用 `audiotestsrc` 当"麦克风"，把解出来的音频丢给 `fakesink` 并数帧，
所以**不需要人、不需要耳朵、不需要麦克风权限**就能回答阶段二那两个问题：

    A. 板子的麦克风 → 这边解出了多少 opus 帧
    B. 这边的测试音 → 板子解出了多少 opus 帧（从板子的 /diag 遥测里读回来）

**为什么要专门写这个**：B 那一路在浏览器上是查不出所以然的。轨没在
setRemoteDescription 之前加进去时，浏览器照样显示"正在发送"，而板子那边
一个帧都解不出来 —— 两边都不报错。只有把两端的计数摆在一起才看得见。

    本地（WSL）跑，让它去连板子 —— 这就是"笔记本浏览器连板子"的真实拓扑：
        python3 tools/selftest.py --signal https://<SIGNAL_HOST> \\
            --room <ROOM> --tls-ca vps-self.crt --duration 20

    --no-send-audio   只测 A（收板子的音频），B 那一路不建

退出码：0 全过；1 判据没过；2 环境/连接问题。
"""

import argparse
import asyncio
import json
import logging
import ssl
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from sdpview import find as sdp_find, describe as sdp_describe  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")

from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

log = logging.getLogger("selftest")

PT_AUDIO = 111
AUDIO_RATE = 48000

# 我们自己发出去的音频 SSRC。**必须是定值**，而且必须在 answer 里写明。
#
# 板子的 webrtcbin 是 **offerer**，它把进来的 RTP 认到某条 transceiver 上靠的是
# **SSRC**，而这个对应关系它只能从远端 SDP 的 `a=ssrc:` 行里读 —— RTP 包里除了
# SSRC 没有任何东西能说明"我是哪一路"。GStreamer 自己当 answerer 时**不写这一行**
# （板子的 offer 有 a=ssrc，GStreamer 回的 answer 没有），于是板子那边只有一句
#
#     WARN webrtcbin gstwebrtcbin.c:6468:on_rtpbin_pad_added:
#          Could not find ssrc 3909431245
#
# 然后**静默放弃**：不建回程链、不报错、日志里连一行中文都没有。
# Chrome / Firefox 的 answer 是带 a=ssrc 的，所以这里主动补上 —— 让这个无头
# 对端在这一点上和浏览器一致，否则测出来的是"GStreamer 对 GStreamer"的怪癖，
# 不是产品的问题。
SSRC_AUDIO = 2882343476

# 收流的两条链。**按 caps 分派**，不能"第一个 pad 就给它"——
# 板子的 src pad 上视频和音频都会来，谁先到不确定。
RECV_SPEC = {
    "video": [("vq", "queue", {}),
              ("vdepay", "rtph264depay", {}),
              ("vparse", "h264parse", {}),
              ("vsink", "fakesink", {"sync": False})],
    "audio": [("aq", "queue", {}),
              ("adepay", "rtpopusdepay", {}),
              ("adec", "opusdec", {}),
              ("asink", "fakesink", {"sync": False})],
}


def request_pad(element, name):
    if hasattr(element, "request_pad_simple"):
        return element.request_pad_simple(name)
    return element.get_request_pad(name)


class Peer:
    """一个无头观看端。"""

    def __init__(self, loop, args, ice):
        self.loop = loop
        self.args = args
        self.ice = ice
        self.pipeline = None
        self.webrtcbin = None
        self.ws = None

        # 计数。探针跑在 streaming 线程，只自增。
        self.recv = {"video_packets": 0, "video_bytes": 0,
                     "audio_packets": 0, "audio_bytes": 0,
                     "audio_frames": 0, "video_frames": 0}
        self.sent = {"audio_packets": 0, "audio_bytes": 0}
        self.answer_sdp = None
        self.built = set()

    # ---------------- 建管线 ----------------
    def start(self):
        desc = "webrtcbin name=wbr bundle-policy=max-bundle"

        if not self.args.no_send_audio:
            # 上游为什么要 audioconvert：audiotestsrc 的原生格式是 F32，
            # opusenc 只吃 S16 —— 中间不转就是 not-negotiated。
            desc += (
                " audiotestsrc name=tsrc is-live=true wave=sine freq={freq}"
                " volume=0.3 ! audioconvert ! audioresample !"
                " audio/x-raw,format=S16LE,rate={rate},channels=1 !"
                " opusenc bitrate=32000 !"
                " rtpopuspay name=vpay pt={pt} ssrc={ssrc} !"
                " application/x-rtp,media=audio,encoding-name=OPUS,payload={pt},"
                "clock-rate={rate} !"
                " queue name=vsend max-size-buffers=200 max-size-bytes=0"
                " max-size-time=0"
            ).format(freq=self.args.freq, rate=AUDIO_RATE, pt=PT_AUDIO,
                     ssrc=SSRC_AUDIO)

        self.pipeline = Gst.parse_launch(desc)
        self.webrtcbin = self.pipeline.get_by_name("wbr")
        if self.webrtcbin is None:
            raise RuntimeError("parse_launch 没建出 webrtcbin")

        ice = self.ice
        if ice.get("stun"):
            self.webrtcbin.set_property("stun-server", ice["stun"])
            log.info("STUN: %s", ice["stun"])
        if ice.get("turn"):
            # 服务器已经 percent-encode 过了，别自己拼
            self.webrtcbin.set_property("turn-server", ice["turn"])
            log.info("TURN: %s", ice["turn"])
        if (ice.get("policy") or "all") == "relay":
            self.webrtcbin.set_property("ice-transport-policy", "relay")
            log.info("强制中继")

        if not self.args.no_send_audio:
            vpay = self.pipeline.get_by_name("vpay")
            vpay.get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                self._count_sent)

        self.webrtcbin.connect("pad-added", self._on_pad_added)
        self.webrtcbin.connect("on-ice-candidate", self._on_ice_candidate)

        # 这两个状态只打给人看，判据一条都不看它们 ——
        # ice-connection-state 报 connected 而实际零媒体，是踩过的坑。
        for prop in ("ice-connection-state", "connection-state"):
            self.webrtcbin.connect("notify::" + prop, self._on_state_changed)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # **先停在 PAUSED，等测试音的 sink 接上再 PLAYING。**
        # 直接 PLAYING 的话，audiotestsrc 是个 live 源，它会立刻推数据，而
        # 那时 vsend 队列的下游（webrtcbin 的 sink_1）还没接上 ——
        # 报的是 "streaming stopped, reason not-linked"，然后这个源就**永久
        # 停了**。后面再接上 sink_1 也没用，测试音是哑的，而板子那边看起来
        # 就像"浏览器的麦克风没上来"。
        self.pipeline.set_state(Gst.State.PAUSED)
        log.info("对端 pipeline 已就绪（PAUSED，等接上发送链再 PLAYING），等 offer…")

    def _on_state_changed(self, element, pspec):
        value = element.get_property(pspec.name)
        log.info("[%s] %s", pspec.name, value.value_nick
                 if hasattr(value, "value_nick") else value)

    # ---------------- 计数探针（streaming 线程，只自增） ----------------
    def _count_sent(self, _pad, info):
        if info.type & Gst.PadProbeType.BUFFER_LIST:
            bl = info.get_buffer_list()
            if bl is not None:
                self.sent["audio_packets"] += bl.length()
                self.sent["audio_bytes"] += bl.calculate_size()
            return Gst.PadProbeReturn.OK
        buf = info.get_buffer()
        if buf is not None:
            self.sent["audio_packets"] += 1
            self.sent["audio_bytes"] += buf.get_size()
        return Gst.PadProbeReturn.OK

    def _count_recv(self, kind):
        def probe(_pad, info):
            key = kind
            if info.type & Gst.PadProbeType.BUFFER_LIST:
                bl = info.get_buffer_list()
                if bl is not None:
                    self.recv[key + "_packets"] += bl.length()
                    self.recv[key + "_bytes"] += bl.calculate_size()
                return Gst.PadProbeReturn.OK
            buf = info.get_buffer()
            if buf is not None:
                self.recv[key + "_packets"] += 1
                self.recv[key + "_bytes"] += buf.get_size()
            return Gst.PadProbeReturn.OK
        return probe

    def _count_decoded(self, kind):
        def probe(_pad, info):
            if info.get_buffer() is not None:
                self.recv[kind + "_frames"] += 1
            return Gst.PadProbeReturn.OK
        return probe

    # ---------------- 收流 ----------------
    def _build_recv_chain(self, kind):
        """建收流的一条链，返回它的第一个元素（queue）。"""
        els = []
        for name, factory, props in RECV_SPEC[kind]:
            el = Gst.ElementFactory.make(factory, name)
            if el is None:
                raise RuntimeError("建不出元素 %s（%s）" % (name, factory))
            for k, v in props.items():
                el.set_property(k, v)
            self.pipeline.add(el)
            els.append(el)
        for prev, nxt in zip(els, els[1:]):
            if not prev.link(nxt):
                raise RuntimeError("%s → %s 连不上" % (prev.get_name(), nxt.get_name()))
        # 管线已经是 PLAYING，新元素默认停在 NULL，得单独同步上去。
        for el in els:
            el.sync_state_with_parent()

        # 包计在 depay 的**入口**（就是 webrtcbin 给过来的 RTP），
        # 帧计在解码器的出口 —— 这两个数的差就是"收到包但解不出帧"。
        els[1].get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            self._count_recv(kind))
        els[2].get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._count_decoded(kind))
        return els[0]

    def _on_pad_added(self, _element, pad):
        # **只认 src pad。** `pad-added` 对**任何**新加进来的 pad 都触发，
        # 包括我们自己 request_pad 出来的 sink_1 —— 实测就是这么炸的：
        # sink_1 先到、caps 里写着 media=audio，于是它把 "audio" 这个槽占了，
        # 真正的音频 src pad 到的时候被当成"已经接过"直接丢弃。
        # 症状是这一路一个包都收不到，而日志上写着"已接上 audio 收流链"。
        if pad.get_direction() != Gst.PadDirection.SRC:
            log.debug("pad-added 是 %s（非 src），忽略", pad.get_name())
            return

        caps = pad.get_current_caps()
        text = caps.to_string() if caps else ""
        if not text:
            # 和板子侧同一个坑：caps 为 NULL 时直接 return 会把整路静默丢掉。
            pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                          self._on_probe_until_caps)
            return
        self._attach(pad, text)

    def _on_probe_until_caps(self, pad, info):
        caps = pad.get_current_caps()
        text = caps.to_string() if caps else ""
        if not text:
            return Gst.PadProbeReturn.OK
        pad.remove_probe(info.id)
        GLib.idle_add(self._attach, pad, text)
        return Gst.PadProbeReturn.OK

    def _attach(self, pad, caps_text):
        kind = "audio" if "media=(string)audio" in caps_text.replace(" ", "") else "video"
        if kind in self.built:
            log.debug("已经接过 %s 了，忽略", kind)
            return
        self.built.add(kind)
        try:
            head = self._build_recv_chain(kind)
        except Exception:  # noqa: BLE001
            log.exception("建 %s 收流链失败", kind)
            return
        ret = pad.link(head.get_static_pad("sink"))
        log.info("已接上 %s 收流链: %s", kind, ret)

    def _on_bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error("pipeline 错误: %s | %s", err.message, debug)
        elif message.type == Gst.MessageType.EOS:
            log.warning("EOS")

    # ---------------- 信令 ----------------
    def _on_ice_candidate(self, _element, mlineindex, candidate):
        self._send_soon({"type": "candidate", "candidate": candidate,
                         "sdpMLineIndex": int(mlineindex)})

    def _send_soon(self, obj):
        asyncio.run_coroutine_threadsafe(self._send(obj), self.loop)

    async def _send(self, obj):
        try:
            if self.ws is not None and not self.ws.closed:
                await self.ws.send_json(obj)
        except (ConnectionResetError, RuntimeError):
            pass

    def on_signal(self, data):
        kind = data.get("type")
        if kind == "offer":
            log.info("收到 offer，生成 answer…")
            self._set_remote_offer(data["sdp"])
        elif kind == "candidate":
            if data.get("candidate"):
                self.webrtcbin.emit("add-ice-candidate",
                                    int(data.get("sdpMLineIndex") or 0),
                                    data["candidate"])

    def _announce_ssrc(self, sdpmsg):
        """往 answer 的音频节里补 `a=ssrc:` 行（浏览器会写，GStreamer 不写）。

        必须在 **set-local-description 和发信令之前** 做：板子是从这段 SDP 里
        建立"这个 SSRC 属于哪条 transceiver"的，补晚了它收到的 RTP 就认不出来
        （症状见 SSRC_AUDIO 上面那段）。返回补上的行数，0 = 没找到音频节。
        """
        n = 0
        for i in range(sdpmsg.medias_len()):
            media = sdpmsg.get_media(i)
            if media.media != "audio":
                continue
            # 两行都写：cname 是 RTP 规范要求的，msid 把流和 track 关联起来。
            # 板子只用到 SSRC 本身，但写成浏览器那样，将来换别的对端也不会
            # 因为少了哪一行而表现不一样。
            media.add_attribute("ssrc", "%d cname:selftest@localhost" % SSRC_AUDIO)
            media.add_attribute("ssrc", "%d msid:selftest audio0" % SSRC_AUDIO)
            n += 2
        return n

    def _set_remote_offer(self, sdp_text):
        if self.args.verbose:
            # 和 answer 那边对称：-v 时把收到的 offer 也原样打出来。板子
            # （offerer）和自己（answerer）的 SDP 差在哪一行，是判断"到底谁
            # 少了什么"的唯一办法。
            for line in (sdp_text or "").splitlines():
                log.debug("  offer| %s", line)

        res, sdpmsg = GstSdp.SDPMessage.new()
        if res != GstSdp.SDPResult.OK:
            log.error("SDPMessage.new 失败: %s", res)
            return
        # 显式比较：GST_SDP_OK == 0 在 Python 里是 falsy，写成 `if not res`
        # 会永远成立然后直接 return。
        res = GstSdp.sdp_message_parse_buffer(sdp_text.encode("utf-8"), sdpmsg)
        if res != GstSdp.SDPResult.OK:
            log.error("SDP 解析失败: %s", res)
            return

        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, sdpmsg)
        promise = Gst.Promise.new()
        self.webrtcbin.emit("set-remote-description", offer, promise)
        promise.interrupt()

        # **发音频的 sink pad 必须等到 set-remote-description 之后再请求**：
        # sink_N 的 N 就是 m-line 下标，而 m-line 是先由板子的 offer 定下来的。
        # 远端描述到手之前请求 sink_1，webrtcbin 手上还没有第 1 条 m-line
        # 可以挂，这一路的方向会算错（answer 里退成 recvonly，测试音发不出去）。
        if not self.args.no_send_audio:
            self._attach_send_audio()

        # 发送链接好了，可以放行了（见 start() 里为什么先停在 PAUSED）。
        self.pipeline.set_state(Gst.State.PLAYING)

        promise = Gst.Promise.new_with_change_func(
            self._on_answer_created, self.webrtcbin, None)
        self.webrtcbin.emit("create-answer", None, promise)

    def _attach_send_audio(self):
        src = self.pipeline.get_by_name("vsend").get_static_pad("src")
        sink = request_pad(self.webrtcbin, "sink_1")
        if sink is None:
            log.error("拿不到 webrtcbin 的 sink_1，测试音发不出去")
            return
        ret = src.link(sink)
        log.info("测试音已接到 webrtcbin 的 sink_1: %s", ret)

    def _on_answer_created(self, promise, element, _u):
        promise.wait()
        reply = promise.get_reply()
        answer = reply.get_value("answer")
        if answer is None:
            log.error("create-answer 返回空")
            return
        # **sdpmsg 只能取一次。** `answer.sdp` 是个 GI 包装过的 boxed 结构，
        # 每次访问属性都会拿到**另一个包装**；往其中一个 add_attribute，
        # 改的是那个临时包装，as_text() 出来的还是没有这行的原文 ——
        # 而返回值会老老实实说"加了 2 行"。这个坑不报错、不改行为，
        # 只是让你以为实验做了、其实没做。
        sdpmsg = answer.sdp

        if not (self.args.no_announce_ssrc or self.args.no_send_audio):
            got = self._announce_ssrc(sdpmsg)
            if got:
                log.info("已在 answer 的音频节补上 %d 行 a=ssrc:%d"
                         "（GStreamer 当 answerer 时不写，浏览器会写）",
                         got, SSRC_AUDIO)
            else:
                log.error("answer 里没有音频节，补不上 a=ssrc —— 板子的 offer 里"
                          "就没有音频 m-line")

        self.answer_sdp = sdpmsg.as_text()

        if self.args.verbose:
            # -v 时把整段 answer 原样打出来。排查「板子收不到我们的音频」时，
            # a=mid / a=ssrc / a=rtcp-mux 这几行是唯一的现场证据：板子的
            # webrtcbin 就是拿它们把进来的 RTP 认到某条 transceiver 上的，
            # 少了哪一行它只会打一句 "Could not find ssrc N" 然后静默放弃。
            for line in self.answer_sdp.splitlines():
                log.debug("  answer| %s", line)

        promise = Gst.Promise.new()
        element.emit("set-local-description", answer, promise)
        promise.interrupt()

        aur = sdp_find(self.answer_sdp, "audio")
        if aur is None:
            log.warning("我们回的 answer 里没有音频节 —— 板子的 offer 里就没有")
        else:
            direction, pt, codecs = sdp_describe(aur)
            log.info("answer 的音频节: 方向=%s PT=%s 编码=%s",
                     direction, pt, ", ".join(codecs) or "?")
            if not self.args.no_send_audio and direction not in ("sendrecv", "sendonly"):
                log.error("!! 方向是 %s —— 我们的测试音发不到板子。"
                          "这一路必然是静的，先别急着怀疑板子。", direction)

        self._send_soon({"type": "answer", "sdp": self.answer_sdp})
        log.info("answer 已发送")

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None


# --------------------------------------------------------------------------- #
async def fetch_ice(session, base):
    """取 STUN/TURN 配置。

    **键名不是 stun/turn**：服务器下发的是 `stunUriForGst` / `turnUriForGst`
    （给 GStreamer 直接吃的 URI 形式，user/pass 已经 percent-encode 过）。
    照自己的直觉写 `cfg["stun"]` 会拿到空串，然后**静默退化成只用 host
    candidate** —— 局域网下照样能通，所以这个错很难被发现，
    一直到换到公网才发现打洞失败。
    """
    url = base + "/ice.json"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                log.warning("取 %s 回 HTTP %d，按无 STUN/TURN 处理", url, r.status)
                return {}
            cfg = await r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("取 %s 失败（%s），按无 STUN/TURN 处理", url, exc)
        return {}

    ice = {"stun": cfg.get("stunUriForGst", ""),
           "turn": cfg.get("turnUriForGst", "")}
    log.info("已取得 ICE 配置（STUN=%s TURN=%s）",
             "有" if ice["stun"] else "无", "有" if ice["turn"] else "无")
    return ice


async def read_device_counters(session, base, room):
    """从信令服务器的 /diag 读回板子的遥测。

    B 那一路的判据只能这么拿：板子解出了多少 opus 帧，只有它自己知道，
    而浏览器侧 getStats 的 outbound-rtp 在"发了但没人收"时一样是涨的。
    """
    url = "%s/diag?room=%s" % (base, quote(room))
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                log.warning("读 /diag 回 HTTP %d（板子的遥测拿不到）", r.status)
                return None
            payload = await r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("读 /diag 失败: %s", exc)
        return None

    # /diag 返回的是 {"items": [...]}，**不是**一个裸列表。当成列表去迭代，
    # 拿到的是字典的键（字符串 'items'），然后 s.get 就炸在 str 上 ——
    # 报错点在下面几行，和真正的原因隔着一段距离。
    samples = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(samples, list):
        log.warning("/diag 的 items 不是列表: %r", type(samples))
        return None

    dev = [s for s in samples if isinstance(s, dict) and s.get("role") == "device"]
    if not dev:
        log.warning("/diag 里没有 role=device 的样本 —— 板子没在跑，"
                    "或者它的 /diag 和这里读的不是同一台服务器")
        return None
    return dev[-1]


def base_http_of(signal):
    """把 --signal 归一化成 https://host[:port] 这样的根地址。

    信令 WebSocket 是 /ws/view，ICE 配置和遥测在同一个服务的根上。允许用户
    直接给 ws://.../ws/publish 那种完整地址，省得来回换算。
    """
    p = urlsplit(signal)
    scheme = {"wss": "https", "ws": "http"}.get(p.scheme, p.scheme)
    return "%s://%s" % (scheme, p.netloc)


async def amain(args):
    base = base_http_of(args.signal)
    scheme = urlsplit(base).scheme
    ws_url = "%s://%s/ws/view?room=%s" % (
        {"https": "wss", "http": "ws"}[scheme],
        urlsplit(base).netloc, quote(args.room))
    log.info("信令: %s", ws_url)

    ctx = None
    if scheme == "https":
        ctx = ssl.create_default_context(cafile=args.tls_ca) if args.tls_ca \
            else ssl.create_default_context()
        if args.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

    conn = aiohttp.TCPConnector(ssl=ctx)
    async with aiohttp.ClientSession(connector=conn) as session:
        ice = await fetch_ice(session, base)

        loop = asyncio.get_running_loop()
        peer = Peer(loop, args, ice)
        peer.start()

        try:
            ws = await session.ws_connect(ws_url, heartbeat=20,
                                          max_msg_size=4 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001
            log.error("连不上信令服务器: %s", exc)
            peer.stop()
            return 2
        peer.ws = ws
        log.info("信令已连接")

        # 信令循环单独跑，主协程等 duration 到点。
        async def signal_loop():
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except Exception:  # noqa: BLE001
                    continue
                if data.get("type") in ("waiting", "error", "publisher-gone"):
                    log.warning("服务器: %s", data.get("message") or data.get("type"))
                peer.on_signal(data)

        sig = asyncio.create_task(signal_loop())

        try:
            await asyncio.sleep(args.duration)
        finally:
            sig.cancel()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

        # 板子的计数要在会话还活着的时候读（会话一断它就不涨了，
        # 但样本还在 /diag 里，所以其实什么时候读都行）。
        dev = await read_device_counters(session, base, args.room)
        peer.stop()

    return report(args, peer, dev)


def report(args, peer, dev):
    log.info("=" * 66)
    log.info("对端（这边，相当于浏览器）")
    log.info("  收到视频  %d 包 / %d 帧 / %.2f MiB",
             peer.recv["video_packets"], peer.recv["video_frames"],
             peer.recv["video_bytes"] / 1048576)
    log.info("  A 收音频  %d 包 / %.2f MiB / **解出 %d 帧**",
             peer.recv["audio_packets"], peer.recv["audio_bytes"] / 1048576,
             peer.recv["audio_frames"])
    if not args.no_send_audio:
        log.info("  B 发音频  %d 包 / %.2f MiB", peer.sent["audio_packets"],
                 peer.sent["audio_bytes"] / 1048576)
    log.info("-" * 66)

    fails = []

    if peer.recv["video_frames"] == 0:
        fails.append("这边一帧视频都没解出来")
    else:
        log.info("OK  视频链路通（解出 %d 帧）", peer.recv["video_frames"])

    # A：板子麦克风 → 这边。判据是**解出的帧**，不是收到的包 ——
    # 包在涨而帧为 0 说明 depay/解码那一段配错了。
    if peer.recv["audio_frames"] > 0:
        log.info("OK  A 路通：板子的麦克风音频解出了 %d 帧", peer.recv["audio_frames"])
    elif peer.recv["audio_packets"] > 0:
        fails.append("A 路收到 %d 个音频包但一帧都没解出来（depay/opusdec 那一段）"
                     % peer.recv["audio_packets"])
    else:
        fails.append("A 路一个音频包都没收到（板子的麦克风那一路没推上来）")

    if args.no_send_audio:
        log.info("--no-send-audio：跳过 B 路（浏览器→板子）")
    elif dev is None:
        log.warning("读不到板子的遥测，B 路无法判定 —— 不算失败，"
                    "但要判定它必须让板子 POST 到同一台信令服务器")
    else:
        got = dev.get("audio_frames_decoded")
        sent = dev.get("audio_packets_out")
        if got is None:
            log.warning("板子的遥测里没有 audio_frames_decoded 字段"
                        "（板子上跑的是旧代码？）")
        elif got > 0:
            log.info("OK  B 路通：板子解出了 %d 个 opus 帧（这就是"
                     "「浏览器的声音真的到了板子」的硬证据）", got)
        else:
            fails.append("B 路板子一个 opus 帧都没解出来"
                         "（板子的 audio_frames_decoded=%s）。"
                         "先看它启动日志里有没有「回程链已就绪」，"
                         "再看上面的 answer 音频方向是不是 sendrecv。" % got)
        log.info("  板端计数: %s", dev)

    log.info("=" * 66)
    if fails:
        for f in fails:
            log.error("失败: %s", f)
        return 1
    log.info("全部判据通过")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="无头 WebRTC 对端：验证双向音频（不需要浏览器/麦克风/耳朵）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--signal", default="https://124.223.11.112",
                   help="信令服务器根地址（ws:// 或 wss:// 的完整地址也认）")
    p.add_argument("--room", required=True)
    p.add_argument("--tls-ca", default=None, help="自签证书的路径（信任锚）")
    p.add_argument("--insecure", action="store_true",
                   help="关掉证书校验。仅用于本地排查 —— 这是关掉校验，"
                        "不是「绕过签名」的正规做法")
    p.add_argument("--duration", type=float, default=20.0,
                   help="连上后跑多久才下结论。太短会把启动斜坡当成失败")
    p.add_argument("--freq", type=float, default=440.0, help="测试音频率")
    p.add_argument("--no-send-audio", action="store_true",
                   help="不建发送那一路，只测 A（板子→这边）")
    p.add_argument("--no-announce-ssrc", action="store_true",
                   help="不在 answer 里补 a=ssrc（复现 GStreamer 裸 answer 那个坑，"
                        "板子会报 Could not find ssrc 然后收不到音频）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    Gst.init(None)
    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, name="glib-main", daemon=True).start()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130
    finally:
        glib_loop.quit()


if __name__ == "__main__":
    sys.exit(main())
