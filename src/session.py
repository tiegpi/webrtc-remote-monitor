#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session.py —— 一路 WebRTC 会话：pipeline + webrtcbin + 信令

一个观看端 = 一个 DeviceSession。本项目按遥操作场景设计，**同时只允许一个**，
所以这里没有 viewer id 那套东西（见 docs/architecture.md 的取舍说明）。

线程模型是这块最容易踩的地方，先讲清楚：

    ┌─ 主线程 ──────────── asyncio 事件循环，跑 WebSocket 收发
    │                        on_signal() 在这里被调用
    │
    └─ glib-main 线程 ──── GLib.MainLoop，GStreamer 的回调都在这里

GStreamer 的信号回调**只能**在 GLib 线程里跑，而 WebSocket 只能在 asyncio
线程里写。两个方向的跨越各有一处，且只有一处：

    asyncio → GLib   on_signal() 里 emit webrtcbin 的信号，会同步派发到 GLib 线程
    GLib → asyncio   _send_threadsafe() 用 run_coroutine_threadsafe 投回去

不要在别的任何地方跨线程碰对方的对象。
"""

import asyncio
import logging

import gi

# 必须在 import 之前指名版本，否则 PyGI 每个模块吐一行 PyGIWarning。
# 只是噪声，但每个工具、每次 --help 都刷三行，演示时很难看，
# 而且它会盖住真正要看的输出。
gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")

from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

import pipeline

log = logging.getLogger("session")

# 统计探针要同时接这两种。理由见 _attach_counters 的说明 —— 少一个就会漏掉
# 九成流量，而且漏得"很有规律"，看起来像正常的小流量。
_RTP_PROBE_TYPES = Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST


class DeviceSession:
    """板子侧的一路会话。

    这里刻意**不持有 WebSocket**，而是持有一个注入进来的 send_fn（协程函数）。
    这样同一套会话代码既能配信令服务器的 WebSocket，也能配别的传输，
    发送那一步是唯一的差异点。
    """

    def __init__(self, send_fn, loop, args, ice=None):
        self.send_fn = send_fn
        self.loop = loop                 # asyncio 事件循环，用于跨线程投递
        self.args = args
        self.ice = ice or {}

        self.pipeline = None
        self.webrtcbin = None
        self.wb = None                   # 白平衡桥（--camera-wb 没开时是 None）
        self.stopped = False

        # 统计量。读它们的是 1Hz 遥测和 tools/analyze_diag.py。
        # RTP 包在**探针回调里**计数，那个回调跑在 streaming 线程上，
        # 所以这里只做自增，不做任何分配或加锁。
        self.n_packets_out = 0
        self.n_bytes_out = 0

    # ---------------- 生命周期 ----------------
    def start(self):
        self.pipeline, self.webrtcbin, self.wb = pipeline.build(self.args, self.ice, log)

        # 顺序有讲究：先接 on-ice-candidate / on-negotiation-needed，
        # 再 set_state(PLAYING)。反过来的话首次协商会因为没人接
        # on-negotiation-needed 而卡住，表现为浏览器一直停在"等待推流端"。
        self.webrtcbin.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtcbin.connect("on-negotiation-needed", self._on_negotiation_needed)

        # 这三个状态是排查"连上了但没画面"的关键观测点，直接打在终端里。
        # 注意：它们**只用来给人看**，自动化判据一条都不看它们 ——
        # ice-connection-state 显示 connected 而实际零媒体，是实测踩过的坑。
        self.webrtcbin.connect("notify::ice-gathering-state", self._on_gathering_state)
        self.webrtcbin.connect("notify::ice-connection-state", self._on_ice_state)
        self.webrtcbin.connect("notify::connection-state", self._on_conn_state)

        self._attach_counters()

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)

        # 白平衡桥的线程要等 pipeline 真的跑起来再起：它一上来就 try_pull_sample，
        # 而 pipeline 还在 READY 的话 appsink 的 is_eos() 是 True（实测），
        # 线程会立刻判定"流结束了"自己收工，白平衡就静默失效了。
        if self.wb is not None:
            self.wb.start()

        log.info("pipeline 已启动，等待协商…")

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.pipeline is not None:
            # 先 NULL 再收桥线程：NULL 会把 appsrc flush 掉，卡在满队列上的
            # push_buffer 立刻返回，线程不用等超时。
            self.pipeline.set_state(Gst.State.NULL)
            if self.wb is not None:
                self.wb.stop()
                self.wb = None
            self.pipeline = None
        self.webrtcbin = None
        log.info("会话已关闭（共发出 %d 包 / %.2f MiB）",
                 self.n_packets_out, self.n_bytes_out / 1048576)

    # ---------------- 统计探针 ----------------
    def _attach_counters(self):
        """在 RTP 打包器的 src pad 上数包和字节。

        为什么打在 rtph264pay 的 src 上、而不是 v4l2src 或编码器上：
        这里量到的就是**将要交给 webrtcbin 的 RTP 字节**，和浏览器 getStats()
        里 inbound-rtp 的 bytesReceived 是同一个口径，两者相除才有意义
        （旧项目实测这个比值是 1.00 / 0.99）。打在原始帧上算不出守恒。

        **必须同时注册 BUFFER 和 BUFFER_LIST**，这是踩过的坑：
        rtph264pay 把一个编码帧切出的几十个 RTP 包攒成 GstBufferList，
        用 gst_pad_push_list() 整批推出去。只注册 BUFFER 的探针不匹配这个类型，
        于是九成流量从眼皮底下过去了。实测 10 秒：

            打包器出口，只 BUFFER       探针被调用   18 次
            打包器出口，BUFFER|LIST     探针被调用  220 次 / 1914 个包
            队列出口（对照）            探针被调用 1914 次 / 1914 个包

        少算 100 倍，而且少算得**很有规律** —— 遥测里表现为"每 6 秒恰好
        +4 个包 / +106 字节"，像心跳一样整齐，完全不像坏了，很容易被判成
        "探针挂错地方"然后反复换位置。真正的原因不是位置。

        注意队列（queue）会把缓冲列表拆成单个缓冲再推，所以打在队列**下游**
        用只 BUFFER 的探针反而看不出问题 —— 这个 bug 只在打包器出口暴露，
        换个位置测就"好了"，很容易被误判成修好了。
        """
        pay = self.pipeline.get_by_name("vpay")
        if pay is None:
            log.warning("找不到 vpay 元素，字节守恒将无法计算")
            return
        pay.get_static_pad("src").add_probe(_RTP_PROBE_TYPES, self._count_out)

    def _count_out(self, _pad, info):
        # streaming 线程上跑。只做自增，绝不在这里拿锁或记日志。
        #
        # 先按 info.type 分派，**不能**直接试 get_buffer_list() 看它返不返 None：
        # 对一个不是缓冲列表的探针调用，会打出
        #   gst_pad_probe_info_get_buffer_list: assertion 'info->type &
        #   GST_PAD_PROBE_TYPE_BUFFER_LIST' failed
        # 那是未定义行为，不是"返回 None 让我退回去走另一条分支"。
        if info.type & Gst.PadProbeType.BUFFER_LIST:
            bl = info.get_buffer_list()
            if bl is not None:
                # calculate_size() 一次给出整条列表的总字节，比在回调里
                # for 循环逐个建 Python 缓冲对象快得多 —— 这个回调每秒
                # 要跑两百多次，而且跑在 streaming 线程上。
                self.n_packets_out += bl.length()
                self.n_bytes_out += bl.calculate_size()
            return Gst.PadProbeReturn.OK

        buf = info.get_buffer()
        if buf is not None:
            self.n_packets_out += 1
            self.n_bytes_out += buf.get_size()
        return Gst.PadProbeReturn.OK

    def counters(self):
        """给遥测用的快照。"""
        return {
            "video_packets_out": self.n_packets_out,
            "video_bytes_out": self.n_bytes_out,
        }

    # ---------------- GStreamer 总线 ----------------
    def _on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error("GStreamer 错误: %s", err.message)
            if debug:
                log.error("  调试信息: %s", debug)

            src = message.src.get_name() if message.src else "?"
            if src == "v4l2src0":
                self._explain_camera_failure()
            elif "not-negotiated" in (err.message or ""):
                log.error(
                    "  协商失败通常是 caps 和设备实际能力对不上。"
                    "确认 %s 支持 %dx%d@%d 的 %s（用 v4l2-ctl --list-formats-ext -d %s 看）",
                    self.args.camera, self.args.width, self.args.height,
                    self.args.fps, pipeline.VIDEO_FORMAT, self.args.camera,
                )
        elif t == Gst.MessageType.EOS:
            log.info("管线走到 EOS（摄像头是 live 源，正常不该出现）")

    def _explain_camera_failure(self):
        """v4l2src 报错时，把设备实际支持的格式列出来。

        没有这段的话，用户只能看到一句 "Failed to allocate required memory"，
        而那句话和真正的原因（格式不支持 / 多平面节点被 libv4l2 拦截）差得很远。
        """
        import subprocess

        log.error("  摄像头 %s 打不开。它实际支持的格式：", self.args.camera)
        try:
            out = subprocess.run(
                ["v4l2-ctl", "--list-formats-ext", "-d", self.args.camera],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            log.error("  （没有 v4l2-ctl，装 v4l-utils 可以看到）")
            return
        for line in (out.stdout or "").splitlines():
            if line.strip():
                log.error("      %s", line.rstrip())

    # ---------------- 协商 ----------------
    def _on_negotiation_needed(self, element):
        log.info("需要协商，创建 offer…")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, element, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, element, _user_data):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        if offer is None:
            log.error("create-offer 返回空")
            return

        promise = Gst.Promise.new()
        element.emit("set-local-description", offer, promise)
        # 官方示例的做法：不 wait() 就 interrupt()，避免阻塞 GLib 主循环。
        promise.interrupt()

        log.info("offer 已生成并下发")
        self._send_threadsafe({"type": "offer", "sdp": offer.sdp.as_text()})

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        self._send_threadsafe({
            "type": "candidate",
            "candidate": candidate,
            "sdpMLineIndex": int(mlineindex),
        })

    def _set_remote_answer(self, sdp_text):
        # 坑：GstSdp 返回的是 GstSDPResult 枚举，而 GST_SDP_OK == 0，
        # 在 Python 里是 **falsy**。写成 `if not res:` 会永远成立，
        # 函数直接 return，set-remote-description 永远不执行 ——
        # 症状是浏览器连上了但一直黑屏，而且没有任何报错。必须显式比较。
        res, sdpmsg = GstSdp.SDPMessage.new()
        if res != GstSdp.SDPResult.OK:
            log.error("SDPMessage.new 失败: %s", res)
            return

        res = GstSdp.sdp_message_parse_buffer(sdp_text.encode("utf-8"), sdpmsg)
        if res != GstSdp.SDPResult.OK:
            log.error("SDP 文本解析失败: %s", res)
            return

        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
        )
        promise = Gst.Promise.new()
        self.webrtcbin.emit("set-remote-description", answer, promise)
        promise.interrupt()
        log.info("已设置远端 answer，ICE 开始打洞")

    # ---------------- 状态观测（给人看，不参与判据） ----------------
    @staticmethod
    def _nick(value):
        return value.value_nick if hasattr(value, "value_nick") else str(value)

    def _on_gathering_state(self, element, _pspec):
        log.info("ICE 收集状态 -> %s",
                 self._nick(element.get_property("ice-gathering-state")))

    def _on_ice_state(self, element, _pspec):
        state = self._nick(element.get_property("ice-connection-state"))
        log.info("ICE 连接状态 -> %s", state)
        if state == "failed":
            log.error(
                "ICE 打洞失败。排查顺序：\n"
                "  1) 两边在同一个网段吗（板子 %s）\n"
                "  2) 防火墙放行高位 UDP 了吗\n"
                "  3) 注意：这个字段报 connected 也**不代表有媒体**，"
                "判据要看候选对的字节有没有在涨",
                pipeline.guess_local_ip(),
            )

    def _on_conn_state(self, element, _pspec):
        log.info("PeerConnection 状态 -> %s",
                 self._nick(element.get_property("connection-state")))

    # ---------------- 信令（在 asyncio 线程被调用） ----------------
    def on_signal(self, data):
        kind = data.get("type")
        log.debug("收到信令: %s", kind)
        try:
            if kind == "answer":
                self._set_remote_answer(data["sdp"])
            elif kind == "candidate":
                cand = data.get("candidate")
                if cand and self.webrtcbin is not None:
                    self.webrtcbin.emit(
                        "add-ice-candidate", int(data.get("sdpMLineIndex") or 0), cand
                    )
            elif kind == "bye":
                self.stop()
            else:
                log.debug("忽略未知信令: %s", kind)
        except Exception:  # noqa: BLE001
            # 这里必须抓：不抓的话一次 set-remote-description 失败会静默断链，
            # 表现成"浏览器连上了但永远没有画面"，排查起来毫无线索。
            log.exception("处理 %s 信令失败", kind)

    # ---------------- 把消息从 GLib 线程投递回 asyncio ----------------
    def _send_threadsafe(self, obj):
        if self.stopped:
            return
        asyncio.run_coroutine_threadsafe(self._send(obj), self.loop)

    async def _send(self, obj):
        if self.send_fn is None:
            return
        try:
            await self.send_fn(obj)
        except (ConnectionResetError, RuntimeError):
            pass
