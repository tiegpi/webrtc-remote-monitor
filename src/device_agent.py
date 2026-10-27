#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_agent.py —— 板子侧的发布端

它在整个系统里只做一件事：**主动连出去**，把自己的画面推给连上来的浏览器。

    浏览器 ──WS──┐
                  ├── signal_server.py ── 撮合，媒体不经过它
    本进程 ──WS──┘

因为是自己连出去，板子上**一个入站端口都不需要开**，NAT 后面、4G 网里都能用。
这也是复用现成信令服务器的直接好处：本进程不用再兼做服务器。

用法：
    # 局域网：信令服务器跑在本机
    python3 src/device_agent.py --room demo --token hunter2

    # 公网：信令服务器在 VPS 上
    python3 src/device_agent.py --signal ws://1.2.3.4:8443/ws/publish \\
        --room demo --token hunter2
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web
from gi.repository import GLib

import audio             # 音频设备探测：判错设备不会报错，只会推一路本底噪声
import camera            # 画质兜底：板子没有 3A，亮度只能自己闭环
import detect            # 人像检测：板端 NPU，页面只负责画框
import pipeline          # 必须早于 Gst.init()：它会在导入时清掉 BSP 埋的环境变量
import sources           # 板子上有哪些摄像头、各自要什么
import whitebalance      # 画质兜底：同一根因的另一半 —— ISP 没跑 AWB，画面偏绿
from session import DeviceSession

log = logging.getLogger("agent")

HERE = Path(__file__).resolve().parent


class FatalSignalError(RuntimeError):
    """参数类错误（房间号/token 不对）。重连解决不了，直接退出。"""


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def http_base_of(ws_url):
    """ws://host:port/ws/publish  ->  http://host:port

    遥测要 POST 到 HTTP 那侧，所以得把 ws(s) 换回 http(s)。
    """
    p = urlsplit(ws_url)
    scheme = {"ws": "http", "wss": "https"}.get(p.scheme, p.scheme)
    return "%s://%s" % (scheme, p.netloc)


def make_ssl_context(args):
    """给 wss:// 用的 SSL 上下文。

    板子自己起 HTTPS 时用的是**自签证书**，默认的信任库里没有它，必须显式
    把那张证书当信任锚加载进来。

    这里**不要**图省事写 `ssl=False` 或者 `verify_ssl=False`：那不是"绕过校验"，
    是"关掉校验"。证书 SAN 配错了它照样连得上，于是这张证书就白签了，
    而且问题会推迟到浏览器那侧才暴露，排查成本高得多。
    """
    if not (args.signal or "").startswith("wss://"):
        return None
    import ssl

    ctx = ssl.create_default_context(cafile=args.tls_ca or None)
    if args.tls_ca:
        log.info("用 %s 作为信任锚", args.tls_ca)
    return ctx


def signal_auth(args):
    """板子访问 VPS 的 HTTP 端点时用的 HTTP Basic Auth。

    `/ice.json`（TURN 凭证）和 `/diag`（字节守恒遥测）现在都在 nginx 的
    auth_basic 后面。板子是最初的可信中转，得用自己的凭证过这一层 ——
    否则它取不到 TURN 配置、遥测也传不上去，而症状是"画面能看、但字节守恒
    那条判据一直说没拿到设备侧样本"，很容易误判成遥测线程挂了。

    **推流 WebSocket（/ws/publish）不要套这个**：那条路是自己的 `--token`，
    而 GStreamer/aiohttp 的 401 交互和 token 是两回事，混在一起只会更难查。
    """
    if not args.signal_auth:
        return None
    user, _, password = args.signal_auth.partition(":")
    return aiohttp.BasicAuth(user, password)


def quiet_ws_reset(loop):
    """别让 aiohttp 心跳任务在对端消失后抛的 ConnectionResetError 刷满日志。

    aiohttp 3.8 的 WebSocketWriter.ping() 没防住"transport 正在关闭"这种情况，
    对端一消失它就在后台任务里抛 ConnectionResetError，而这个异常没人接。
    功能上无害，但每断一条连接就刷一屏红字，对一个专门用来排查问题的工具来说
    太误导了。心跳本身要留着：网络如果静默断掉（没有 FIN），
    没有心跳就永远发现不了。
    """

    def handler(loop_, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and "closing transport" in str(exc):
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)


# --------------------------------------------------------------------------- #
# 发布端的可变状态
# --------------------------------------------------------------------------- #
class AgentState:
    """跨重连存活的那点状态。

    session 是**当前那一路观看端**（本项目同时只接受一个，见 session.py），
    没有观看端时是 None。source 是"记住的选择"—— 没有观看端时切换摄像头
    只改它，下一个观看端接入时按它建 pipeline。
    """

    def __init__(self, args):
        self.args = args
        self.session = None
        # 当前会话属于哪个观看端。信号服务器支持多个观看端并存，而本进程
        # 只服务一个；所有 viewer-left / answer / candidate 都要拿它做匹配，
        # 否则被拒绝的第二个观看端一离开，就会把第一个的会话拆掉。
        self.viewer_id = None
        self.source = sources.initial(args.camera_source)
        self.lock = asyncio.Lock()      # 串行化切换/检测开关，防页面连点
        # 检测开关是**进程级**的选择：没有观看端时点开关只改它，下一个会话
        # 接入时按这个值建检测线程。
        self.detect_enabled = bool(getattr(args, "detect", False))

    def current_name(self):
        if self.session is not None:
            return self.session.current_source().name
        return self.source.name


# --------------------------------------------------------------------------- #
# 本地控制端点：页面切换摄像头
# --------------------------------------------------------------------------- #
# 为什么不用信令通道把"换个摄像头"从页面发到板子：信令服务器两个方向都是
# **白名单分派**，未知的 type 只在 else 里 log.debug 一句就丢掉（见
# signal_server.py 的 handle_view / handle_publish），要加一条就得改 VPS 上
# 跑着的生产服务。而且这件事**本来就该在本地做** —— 切换摄像头是局域网内的事，
# 绕公网 VPS 一圈意味着 VPS 挂了就换不了摄像头，可板子就在隔壁。
#
# 走 unix socket 而不是 127.0.0.1:端口：只多两行（清残留 + chmod），
# 但保住了"本进程一个入站 TCP 端口都不开"这条性质 —— 对一个强调
# "NAT 后面、4G 网里都能用"的项目不值得为省两行代码去破它。
def _settle(fut, outcome):
    """从别的线程把结果填回 asyncio 的 future。"""
    if fut.done():
        return
    if isinstance(outcome, BaseException):
        fut.set_exception(outcome)
    else:
        fut.set_result(outcome)


async def run_on_glib(fn, *fn_args, timeout=15.0):
    """把一次调用投到 GLib 线程执行并等结果。

    项目里已有的跨线程投递是**反方向**的（GLib → asyncio，见
    session._send_threadsafe）。这里是 asyncio → GLib：`GLib.idle_add` 本身
    线程安全，从事件循环这边丢过去没问题；结果再用 call_soon_threadsafe 回填。

    为什么必须投过去：在 PLAYING 的管线里加删元素会碰 GStreamer 的状态机和
    streaming 线程，从 asyncio 线程直接调是未定义行为。
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def on_idle():
        try:
            outcome = fn(*fn_args)
        except Exception as exc:            # noqa: BLE001
            outcome = exc
        loop.call_soon_threadsafe(_settle, fut, outcome)
        return False                        # 只跑一次

    GLib.idle_add(on_idle)
    return await asyncio.wait_for(fut, timeout)


def socket_path(args):
    p = Path(args.agent_socket)
    return p if p.is_absolute() else (HERE.parent / p)


async def handle_camera(request):
    """GET  = 有哪些摄像头 + 当前是哪个
    POST = 切到某个（{"name": "usb"}）
    """
    state = request.app["state"]

    if request.method == "GET":
        return web.json_response({
            "available": sources.available(),
            "current": state.current_name(),
        })

    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001
        return web.json_response(
            {"ok": False, "error": "请求体不是 JSON"}, status=400)

    name = (body or {}).get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name.strip():
        # 少了 name 直接说清楚。放它往下走的话 resolve(None) 会报
        # "未知摄像头 None"，把"你忘了给字段"说成"你给的名字不对"。
        return web.json_response(
            {"ok": False, "error": '请求体里要有 name 字段，如 {"name": "usb"}'},
            status=400)

    try:
        src = sources.resolve(name)
    except KeyError as exc:
        return web.json_response(
            {"ok": False, "error": sources.message_of(exc),
             "available": sources.available()},
            status=400)

    if not os.path.exists(src.device):
        return web.json_response(
            {"ok": False, "error": "设备 %s 不存在（摄像头没插好？）" % src.device},
            status=409)

    async with state.lock:
        if state.current_name() == src.name and state.session is not None:
            return web.json_response(
                {"ok": True, "current": src.name, "applied": False,
                 "note": "已经在这一路上"})

        # 画质兜底在**换段之前**跑，而且放线程池：亮度闭环要 sleep 好几轮，
        # 别堵事件循环。（此刻旧段还在跑，而它要用的 ISP 节点是空的。）
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, sources.apply_quality, src, state.args)
        except Exception as exc:            # noqa: BLE001
            log.exception("画质兜底失败")
            return web.json_response(
                {"ok": False, "error": "画质兜底失败: %s" % exc}, status=500)

        session = state.session
        if session is None or session.stopped:
            # 没有观看端：只记住选择，下一个观看端接入时按它建 pipeline。
            state.source = src
            return web.json_response(
                {"ok": True, "current": src.name, "applied": False})

        try:
            applied = await run_on_glib(session.switch_source, src.name)
        except Exception as exc:            # noqa: BLE001
            log.exception("切换摄像头失败")
            return web.json_response(
                {"ok": False, "error": str(exc)}, status=500)

        state.source = session.current_source()
        return web.json_response({"ok": True, "current": applied, "applied": True})


async def handle_detect(request):
    """GET  = 检测是否可用 + 当前开关
    POST = {"enabled": true/false}
    """
    state = request.app["state"]
    problem = detect.check_available(state.args.detect_model)

    if request.method == "GET":
        session = state.session
        live = None
        if session is not None and not session.stopped:
            try:
                live = session.detect_state()
            except Exception:                # noqa: BLE001
                live = None
        return web.json_response({
            "available": problem is None,
            "reason": problem,
            "enabled": bool(live["enabled"]) if live else state.detect_enabled,
            "model": state.args.detect_model,
            "fps": state.args.detect_fps,
            "conf": state.args.detect_conf,
            "session": live,
        })

    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001
        return web.json_response(
            {"ok": False, "error": "请求体不是 JSON"}, status=400)

    enabled = (body or {}).get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        # bool 和 int 在 Python 里能混用（True == 1），所以这里显式判类型：
        # 放过去会让 {"enabled": "true"} 静默变成"没开"或"开了"，都不对。
        return web.json_response(
            {"ok": False,
             "error": '请求体里要有 enabled 布尔值，如 {"enabled": true}'},
            status=400)
    if enabled and problem:
        return web.json_response(
            {"ok": False,
             "error": "人像检测不可用: %s" % problem.splitlines()[0],
             "reason": problem},
            status=409)

    # 和摄像头切换共用一把锁：检测线程停/起不能和采集段拆/建交错。
    async with state.lock:
        session = state.session
        applied = False
        if session is not None and not session.stopped:
            try:
                ok = await run_on_glib(session.set_detect, enabled)
            except Exception as exc:         # noqa: BLE001
                log.exception("切换人像检测失败")
                return web.json_response(
                    {"ok": False, "error": "切换人像检测失败: %s" % exc},
                    status=500)
            if not ok:
                # 页面刚连上、pipeline 还没建好时点开关：会话对象在了，但
                # 采集段还没有 detsink。这不是失败 —— 意图已经记在会话上，
                # _start_on_glib 建好管线后会自己开启。
                if session.switch is None:
                    state.detect_enabled = enabled
                    return web.json_response(
                        {"ok": True, "enabled": enabled, "applied": False})
                # 真的开不起来：模型加载/NPU 初始化失败。
                # 把实际状态带回去，页面据此把按钮恢复成"关"。
                state.detect_enabled = False
                return web.json_response(
                    {"ok": False, "enabled": False,
                     "error": "人像检测开不起来，具体原因看 agent 日志"},
                    status=500)
            applied = True
        state.detect_enabled = enabled
        return web.json_response(
            {"ok": True, "enabled": enabled, "applied": applied})


async def start_control(state):
    """起本地控制端点。返回 AppRunner，由调用方在退出时 cleanup。"""
    app = web.Application()
    app["state"] = state
    app.router.add_route("*", "/camera", handle_camera)
    app.router.add_route("*", "/detect", handle_detect)

    path = socket_path(state.args)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 上一次没退干净留下的 socket 文件会让 bind 直接失败（EADDRINUSE），
    # 而报错完全看不出是"残留文件"。
    if path.exists():
        path.unlink()

    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, str(path)).start()
    # 只有本机、而且只有跑 web_server 的那个用户可以调它。
    os.chmod(path, 0o600)
    log.info("控制端点 %s（GET/POST /camera、/detect）", path)
    return runner


# --------------------------------------------------------------------------- #
# 遥测：把板子侧的计数送回服务器，供 tools/analyze_diag.py 算字节守恒
# --------------------------------------------------------------------------- #
async def telemetry_loop(http, args, get_session, base_url, interval=1.0):
    """每秒 POST 一次本机计数。

    为什么走 HTTP 而不是 DataChannel：**链路断了的时候你才最需要这份数据**。
    DataChannel 跑在同一条 WebRTC 链路上，链路一断遥测跟着断，恰好失去作用；
    HTTP 是独立的一条路，媒体挂了它还在，能告诉你板子到底发出去了多少。
    阶段三的命令通道仍然走 DataChannel，职责分开。

    参数是 get_session（可调用对象）而不是 session 本身：会话是观看端接入时
    才建出来的，这个循环启动得比它早，必须每轮重新取。
    """
    url = "%s/diag" % base_url
    while True:
        await asyncio.sleep(interval)
        session = get_session()
        if session is None or session.stopped:
            continue
        try:
            await http.post(url, json={
                "role": "device",
                "room": args.room,
                **session.counters(),
            }, timeout=aiohttp.ClientTimeout(total=3),
               auth=signal_auth(args))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("遥测上报失败（不影响推流）: %s", exc)


# --------------------------------------------------------------------------- #
# 一轮连接
# --------------------------------------------------------------------------- #
async def publish_once(args, state, url, ice_url):
    """连上信令服务器，按需建会话并推流。

    返回 True 表示**正常断开**（网络抖动、对方关连接），调用方可以重置退避。
    返回 False 表示**被服务器拒绝**（room-busy 之类），要保留退避 ——
    否则退避永远停在 1 秒，变成对服务器每秒一次的锤击。
    """
    quiet_ws_reset(asyncio.get_running_loop())
    clean = True
    loop = asyncio.get_running_loop()
    ctx = make_ssl_context(args)

    # 把 ctx 挂在 connector 上，而不是只传给 ws_connect：遥测走的是同一个
    # ClientSession 的普通 HTTP POST，如果只给 WebSocket 那一处传 ssl，
    # 遥测会拿默认信任库去校验自签证书 → 每轮都失败 → 被 telemetry_loop 的
    # except 吞掉。症状是连接一切正常，只有字节守恒那条判据说"没拿到设备侧样本"。
    # ctx 为 None（明文 ws://）时这里是 aiohttp 的默认行为，不受影响。
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=ctx)
    ) as http:
        async with http.ws_connect(url, heartbeat=30, ssl=ctx) as ws:
            log.info("已连上信令服务器: %s", url)
            sender = WsSender(ws)
            telem = asyncio.create_task(
                telemetry_loop(http, args, lambda: state.session, http_base_of(url))
            )
            # lambda 是为了让遥测每轮都读到**当前**的 session 对象，
            # 而不是把建会话之前的 None 捕获死。
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    kind = data.get("type")

                    if kind == "viewer-joined":
                        vid = data.get("viewer")
                        if not vid:
                            log.error("服务器发的 viewer-joined 里没有 viewer 字段，"
                                      "无法路由，跳过这次接入")
                            continue
                        # 单观看端设计：已经有人在看就明确拒绝，
                        # 而不是默默开第二路抢同一个摄像头。
                        # 错误消息**必须带 viewer**：信令服务器靠这个字段
                        # 找连接（signal_server.handle_publish），缺了它消息
                        # 会被当成"发布端发错房间"丢掉，第二个观看端就永远
                        # 停在等待，而这边日志却写着"已拒绝"。
                        if state.session is not None and not state.session.stopped:
                            log.warning("已有一个观看端，拒绝新的连接")
                            await sender.send({
                                "type": "error",
                                "viewer": vid,
                                "code": "device-busy",
                                "message": "本设备同时只接受一个观看端，"
                                           "请等当前观看端退出后刷新页面",
                            })
                            continue
                        ice = await fetch_ice(http, ice_url, args)
                        ice.setdefault("stun", "")
                        ice.setdefault("turn", "")
                        ice["stun"] = ice["stun"] or args.stun
                        ice["turn"] = ice["turn"] or args.turn
                        ice["policy"] = args.ice_policy

                        # source 用 state 里记住的那个：没有观看端时切过摄像头
                        # 的话，新会话要按那个选择建 pipeline。
                        state.session = DeviceSession(
                            tag_viewer(sender, vid), loop, args, ice,
                            source=state.source,
                            detect_enabled=state.detect_enabled,
                        )
                        state.viewer_id = vid
                        try:
                            state.session.start()
                        except Exception as exc:  # noqa: BLE001
                            log.exception("建 pipeline 失败")
                            await sender.send({"type": "error", "message": str(exc)})
                            state.session = None
                            state.viewer_id = None
                            continue
                        log.info("观看端已接入，开始推流")

                    elif kind == "viewer-left":
                        # 只认当前会话的那个观看端。被拒绝的第二个观看端也会
                        # 在断开时触发 viewer-left，如果不比对 id，它会把正常
                        # 那个会话一起停掉 —— 症状是"后连的把先连的踢下线"。
                        left_vid = data.get("viewer")
                        if state.session is None:
                            continue
                        if left_vid is not None and left_vid != state.viewer_id:
                            log.info("观看端 %s 离开，但当前会话属于 %s，忽略",
                                     left_vid, state.viewer_id)
                            continue
                        state.session.stop()
                        state.session = None
                        state.viewer_id = None
                        log.info("观看端已离开")

                    elif kind in ("answer", "candidate"):
                        # 同样只接受当前观看端的信令；否则被拒绝的那个如果
                        # 乱发 answer/candidate，会污染正在跑的会话。
                        msg_vid = data.get("viewer")
                        if msg_vid is not None and msg_vid != state.viewer_id:
                            log.debug("忽略非当前观看端 %s 的 %s", msg_vid, kind)
                            continue
                        if state.session is not None:
                            state.session.on_signal(data)

                    elif kind == "bye":
                        if state.session is not None:
                            state.session.stop()
                            state.session = None
                            state.viewer_id = None

                    elif kind == "error":
                        code = data.get("code")
                        log.error("信令服务器: %s", data.get("message"))
                        if code in ("bad-token", "bad-room"):
                            raise FatalSignalError(data.get("message") or code)
                        clean = False       # room-busy 等：等对方让位，保留退避

                    elif kind == "ice-servers":
                        has = data.get("stunUriForGst") or data.get("turnUriForGst")
                        log.info("收到服务器下发的 ICE 配置（STUN/TURN %s）",
                                 "有" if has else "无，只用 host candidate")

                    else:
                        log.debug("忽略消息: %s", kind)
            finally:
                telem.cancel()
                try:
                    await telem
                except asyncio.CancelledError:
                    pass
                if state.session is not None:
                    state.session.stop()
                    state.session = None
                    state.viewer_id = None
    return clean


def tag_viewer(sender, vid):
    """把发布端发出的每条消息都打上观看端 id。

    信令服务器是**按消息里的 viewer 字段**决定转发给谁的（它本身支持多个
    观看端并存）。本进程虽然只服务一个观看端，但这个字段必须带上：
    少了它服务器查不到对应的观看端，于是把 offer 和所有 candidate 全部丢进
    垃圾桶，只在**服务器**日志里留一行 warning。

    这个 bug 的症状特别有欺骗性 —— 板子这边日志干干净净，"offer 已生成并下发"
    "ICE 收集状态 -> complete" 都打了，浏览器那边也只是静静地不动，
    两边都不会报错。第一版就是卡在这里：服务器日志里刷了 20 行
    "发布端发来 offer，但 viewer None 不存在，忽略"。

    id 是服务器在 viewer-joined 里给的，不需要本进程自己生成或持久化。
    """
    async def send(obj):
        await sender.send({**obj, "viewer": vid})
    return send


class WsSender:
    """给一条 WebSocket 套一层发送锁，把并发写串行化。

    aiohttp 的 WebSocketWriter **没有发送锁**。本进程里 GLib 线程会往这条
    WS 上写 offer 和 candidate，虽然只有一路会话、冲突概率低，但一旦交错
    就会把 JSON 帧写坏，症状是服务器收到半截 JSON 然后整条连接失去响应。
    加锁的代价可以忽略。
    """

    def __init__(self, ws):
        self.ws = ws
        self.lock = asyncio.Lock()
        self.broken = False

    async def send(self, obj):
        if self.broken or self.ws.closed:
            return
        async with self.lock:
            try:
                await self.ws.send_json(obj)
            except (ConnectionResetError, RuntimeError):
                self.broken = True


async def fetch_ice(http, ice_url, args):
    """从信令服务器取 STUN/TURN 配置。取不到就用命令行参数兜底。"""
    if not ice_url:
        return {}
    try:
        async with http.get(ice_url, timeout=aiohttp.ClientTimeout(total=5),
                            auth=signal_auth(args)) as r:
            cfg = await r.json()
        return {
            "stun": cfg.get("stunUriForGst", ""),
            "turn": cfg.get("turnUriForGst", ""),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("取 %s 失败: %s（回退到命令行参数）", ice_url, exc)
        return {}


async def run_publisher(args, state):
    """主循环：断线自动重连（指数退避）。板子和家宽掉线很常见。"""
    sep = "&" if "?" in args.signal else "?"
    url = "%s%sroom=%s&token=%s" % (
        args.signal, sep, quote(args.room, safe=""), quote(args.token, safe="")
    )
    ice_url = args.ice_url or (http_base_of(args.signal) + "/ice.json")

    src = state.source
    log.info("房间 %s，ICE 策略 %s", args.room, args.ice_policy)
    log.info("视频源 %s（%s）  %dx%d@%d  %s  %d kbps",
             src.name, src.device, args.width, args.height, args.fps,
             src.fourcc, args.bitrate // 1000)
    log.info("  可切换的源：%s（页面上有按钮，走板子本地的控制通道）",
             "、".join("%s=%s" % (s.name, s.device) for s in sources.SOURCES))
    if args.no_audio:
        log.info("音频已关闭（--no-audio）：只推视频，和阶段一一样")
    else:
        log.info("麦克风 %s  → 浏览器（Opus %d kbps）",
                 args.mic_device, args.audio_bitrate // 1000)
        log.info("扬声器 %s  ← 浏览器", args.spk_device)
        log.warning("  播放功放开关（hp switch）的回读**不可信**：写 on 会报成功"
                    "但读回来仍是 off。有没有声音只能用耳朵确认："
                    "tools/check_audio.sh --tone")
    if pipeline.LIBV4L2_WAS_FORCED:
        log.info("已清掉 GST_V4L2_USE_LIBV4L2（BSP 在登录 shell 里导出它，"
                 "留着会让 rkisp 分配缓冲失败）")

    delay = 1.0
    while True:
        clean = False
        try:
            clean = await publish_once(args, state, url, ice_url)
            log.info("信令连接已断开")
        except FatalSignalError as exc:
            log.error("服务器拒绝且重试解决不了: %s", exc)
            log.error("检查 --room / --token 是否和服务器启动时打印的一致")
            return 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("与信令服务器断开: %s", exc)

        if clean:
            delay = 1.0
        log.info("%.0f 秒后重连…", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)


async def run_forever(args, state):
    """控制端点和推流循环一起跑。

    **端点必须活在推流循环之外**：run_publisher 自己带重连退避，一次断线不该
    让 socket 跟着消失 —— 断线恰恰是最想去点一下页面看看的时候。
    """
    runner = await start_control(state)
    try:
        return await run_publisher(args, state)
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="板子侧 WebRTC 发布端（RK3588 远程遥操作）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    g = p.add_argument_group("信令")
    g.add_argument("--signal", default="ws://127.0.0.1:8080/ws/publish",
                   help="信令服务器 WebSocket 地址。HTTPS 的服务器用 wss://")
    g.add_argument("--room", default="demo", help="房间名")
    g.add_argument("--token", default="demo", help="推流端 token")
    g.add_argument("--ice-url", default=None,
                   help="STUN/TURN 配置地址，默认取信令服务器同源的 /ice.json")
    g.add_argument("--signal-auth", default=None, metavar="USER:PASS",
                   help="访问 VPS 的 /ice.json、/diag 用的 HTTP Basic Auth。"
                        "**只用于 HTTP 端点**；/ws/publish 仍然只认 --token")
    g.add_argument("--tls-ca", default=None,
                   help="wss:// 且服务器用自签证书时，把那张证书的路径给它")
    g.add_argument("--stun", default="", help="直接指定 STUN，覆盖服务器下发的")
    g.add_argument("--turn", default="", help="直接指定 TURN，覆盖服务器下发的")
    g.add_argument("--ice-policy", default="all", choices=["all", "relay"],
                   help="relay = 强制走 TURN。用来验证中继真的生效")

    c = p.add_argument_group(
        "摄像头（两个源都能用，观看页上可以随时切，见 src/sources.py）。"
        "宽高帧率对**所有**源一视同仁：切换不用重新协商，靠的就是两路编码输出"
        "的 caps 完全一样，所以别想着给某一个源单独设尺寸")
    c.add_argument("--camera-source", default=sources.SOURCES[0].name,
                   choices=sources.names(),
                   help="启动时用哪一路。运行期可以用页面上的按钮换掉它")
    c.add_argument("--agent-socket", default="run/agent.sock",
                   help="本地控制端点的 unix socket（相对路径按项目根算）。"
                        "web_server.py 的 --agent-socket 必须指向同一个地方。"
                        "走 unix socket 而不是 TCP 端口，是为了保住"
                        "「本进程不开任何入站 TCP 端口」这条性质")
    c.add_argument("--width", type=int, default=1280)
    c.add_argument("--height", type=int, default=720)
    c.add_argument("--fps", type=int, default=30)
    c.add_argument("--bitrate", type=int, default=2500000,
                   help="H.264 目标码率(bps)。mpph264enc 的默认值约 3.4Mbps")
    c.add_argument("--gop", type=int, default=10,
                   help="关键帧间隔(帧)。默认 10 —— 这个值不是随手定的，"
                        "整条验收线都压在它上面："
                        "  中途加入的浏览器要等到下一个 IDR 才有画面，"
                        "  切换摄像头也是（画面最多停一个 GOP）；"
                        "  而 60（= 2 秒）实测会让「画面静止时间占比」到 8.5%%，"
                        "  直接超过 analyze_diag.py 的 3%% 上限 —— 也就是说"
                        "  用默认值跑完整回归是**过不了**的。"
                        "带宽换流畅，要省带宽就显式调大，但别指望那时回归还全绿")

    q = p.add_argument_group(
        "画质（板子上没有 rkaiq 3A，AE/AWB 都不会自己跑，所以由本进程兜底）")
    q.add_argument("--camera-ctl", default=None,
                   help="直接写进 sensor subdev 的 V4L2 控件，如 "
                        "'analogue_gain=176,exposure=1648'。"
                        "给了这个就照写、不做亮度闭环 —— 那是明确指定的值，"
                        "替你“优化”掉只会让画面和你说的话对不上")
    q.add_argument("--camera-no-auto", action="store_true",
                   help="不做亮度闭环。画面会停在驱动默认的 gain=16，也就是基本全黑")
    q.add_argument("--camera-target-y", type=float, default=camera.TARGET_Y,
                   help="亮度闭环的目标（折算 Y 均值）。够不着就顶在增益上限上")
    q.add_argument("--camera-exposure", type=int, default=1648,
                   help="闭环开始时把曝光设成这个值。驱动默认 1536，上限 1648")
    q.add_argument("--camera-probe", default=camera.PROBE_DEVICE,
                   help="闭环量亮度用的 ISP 节点。默认 selfpath，"
                        "它和推流用的主通道共用同一个 sensor")
    q.add_argument("--camera-wb", nargs="?", const="auto", default=None,
                   help="修偏绿（ISP 没跑 AWB）。不带值=自校准（灰世界，前 15 帧）；"
                        "也可以直接给 \"R,B\" 两个增益，如 1.71,1.55。不给就不校正")

    d = p.add_argument_group(
        "人像检测（阶段四，板端 NPU + 页面叠加框）。默认关闭，观看页上有开关；"
        "没有观看端时开关只被记住，下一个会话按它生效")
    d.add_argument("--detect", action="store_true",
                   help="启动时直接开启检测（等价于页面上一开始就把开关打开）。"
                        "显式给了但它用不了（模型/依赖缺失）会直接报错退出")
    d.add_argument("--detect-model", default=detect.MODEL_DEFAULT,
                   help="YOLOv5 RKNN 模型路径。模型不入库，换机器时指到实际文件")
    d.add_argument("--detect-fps", type=int, default=5,
                   help="检测采样率（实际会被夹在 1~15）。5fps 下单帧约 30ms，"
                        "只占 NPU 单核约 15%%")
    d.add_argument("--detect-conf", type=float, default=detect.CONF_DEFAULT,
                   help="person 置信度阈值。调低更敏感但误报更多")

    s = p.add_argument_group(
        "音频（阶段二）。不给设备就自动探测，探测顺序见 src/audio.py")
    s.add_argument("--mic-device", default=None,
                   help="采集设备（ALSA 名，如 hw:3,0）。不给就自动探测："
                        "四极耳麦（Headset Mic Jack=on）优先，否则用 USB 采集卡；"
                        "都没有就**明确报错**，不会静默录一路本底噪声")
    s.add_argument("--spk-device", default=None,
                   help="播放设备，默认 3.5mm 那一路。注意板子的播放功放开关"
                        "（hp switch）回读不可信，用 tools/check_audio.sh --tone "
                        "靠耳朵确认有没有声音")
    s.add_argument("--audio-bitrate", type=int, default=32000,
                   help="Opus 目标码率(bps)。语音 32k 足够，再高对语音没意义")
    s.add_argument("--no-audio", action="store_true",
                   help="完全不做音频，退化成阶段一的行为。"
                        "没有麦克风又想演示视频时用这个")

    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # 人像检测的依赖/模型在启动时先验掉：公网模式下每来一个观看端就会建会话，
    # 拖到第一次开检测才发现"模型不存在"会按观看端重复报错，而且容易让人以为
    # 视频链路坏了。**只有显式 --detect 时才把缺失当致命错误**；否则检测只是
    # 一个可选功能，视频该照常跑。
    detect_problem = detect.check_available(args.detect_model)
    if detect_problem:
        if args.detect:
            log.error("--detect 用不了：%s", detect_problem)
            return 2
        log.info("人像检测不可用（不影响视频）：%s", detect_problem.splitlines()[0])
    else:
        log.info("人像检测可用：模型 %s（%d fps，conf %.2f）",
                 args.detect_model, args.detect_fps, args.detect_conf)

    # 白平衡的参数和依赖在建 pipeline 之前先验掉：公网模式下每来一个观看端就会
    # 建一条 pipeline，拖到那时候才发现"板子上没装 numpy"会按观看者数重复报错。
    if args.camera_wb:
        try:
            whitebalance.parse_spec(args.camera_wb)
        except ValueError as exc:
            log.error("%s", exc)
            log.error("用法:  --camera-wb              自校准（灰世界，前 %d 帧）\n"
                      "       --camera-wb 1.71,1.55    手动给 R、B 的增益（G 固定为 1）",
                      whitebalance.WB_CALIB_FRAMES)
            return 2
        problem = whitebalance.check_available()
        if problem:
            log.error("--camera-wb 用不了：%s", problem)
            return 2

    # 音频设备在**启动时**定下来，不是每次建 pipeline 时：公网模式下每来一个
    # 观看端就会建一条 pipeline，拖到那时候才发现"没麦克风"会按观看者数重复报错，
    # 而且报在观看端接入之后，看起来像是接入流程坏了。
    if not args.no_audio:
        try:
            audio.resolve(args, log)
        except audio.AudioUnavailable as exc:
            log.error("%s", exc)
            return 2

    # 画质兜底放在最前面：此刻还没有观看端、pipeline 还没建，摄像头是空的，
    # 量亮度的那个 ISP 节点不会和正在推的流抢资源。
    # 只有 quality_fix 的源（MIPI）才真的跑它 —— USB 摄像头自带 AE/AWB，
    # 跑那套既没意义又会去写 MIPI sensor 的 subdev 控件。
    state = AgentState(args)
    sources.apply_quality(state.source, args, log)

    from gi.repository import Gst

    # Gst.init() 必须在这里 —— pipeline 模块已经在导入时清掉了那个环境变量，
    # 而 v4l2 插件是在创建 element 时读它的。
    Gst.init(None)

    # GLib.MainLoop 跑在守护线程上，asyncio 跑在主线程。
    # GStreamer 的回调全部落在 glib 线程，跨线程投递见 session.py 的说明。
    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, name="glib-main", daemon=True).start()

    try:
        return asyncio.run(run_forever(args, state))
    except KeyboardInterrupt:
        return 0
    finally:
        glib_loop.quit()


if __name__ == "__main__":
    sys.exit(main())
