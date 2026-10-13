#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_server.py —— 信令服务器

它把板子（发布端）和浏览器（观看端）撮合到一起，并在 /ice.json 上下发
STUN/TURN 配置。**媒体本身不经过这里**（除非走 TURN 中继，那时经过同一台
机器上的 coturn）。

    浏览器  ──WS /ws/view────┐                        ┌──WS /ws/publish──  板子
                             ├── signal_server ───────┤
                             └── /ice.json ───────────┘

两个部署位置，同一个文件：

    局域网   跑在**板子上**。板子和浏览器同网段，host candidate 直接连通，
             --ice-mode none 就够，不需要 STUN/TURN
    公网     跑在 VPS 上，配 --public-ip 和 coturn，走 --ice-mode turn

用法：
    # 局域网（板子上）
    python3 src/signal_server.py --room demo --token hunter2 --ice-mode none

    # 公网（VPS）
    python3 src/signal_server.py --public-ip 1.2.3.4 --turn-secret <hex>

浏览器打开 http://<主机>:<端口>/?room=demo
"""

import argparse
import asyncio
import base64
import collections
import errno
import hashlib
import hmac
import json
import logging
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

log = logging.getLogger("signal")

HERE = Path(__file__).resolve().parent


def web_dir():
    """monitor.html 所在目录。

    仓库布局是 src/ + web/ 分开的，而扁平部署时两个文件在同一个目录里。
    两种都要能跑，所以按顺序找。
    """
    return HERE if (HERE / "monitor.html").is_file() else HERE.parent / "web"


# /ice.json 的缓存：凭证是现算的，没必要每个请求都签一次
_ice_cache = {"at": 0.0, "cfg": None}
_ICE_CACHE_SECONDS = 300


# --------------------------------------------------------------------------- #
# ICE / TURN 凭证
# --------------------------------------------------------------------------- #
def build_ice_config(args):
    """生成下发给两端的 ICE 配置。

    --ice-mode none 是局域网用的：两边同网段，host candidate 直接连通，
    根本不需要 STUN/TURN。**这时候一定要真的不配**，不能"配一个反正连不上的" ——
    libnice 会老老实实去连那个不存在的 TURN 服务器然后等超时，
    症状是每次协商都白白慢好几秒，而日志里只有一堆看不懂的 candidate 错误。

    turn 模式下凭证走 coturn 的 use-auth-secret（即常说的 TURN REST API）：
        username   = "<过期时间戳>:<标识>"
        credential = base64(HMAC-SHA1(static-auth-secret, username))
    """
    cfg = {"ttl": args.ice_ttl, "iceServers": []}
    if args.ice_mode == "none":
        # 显式给空数组而不是省略这个字段：浏览器的 RTCPeerConnection 拿到
        # undefined 和拿到 [] 行为一样，但空数组在 /ice.json 里一眼能看出
        # "确实是没有"，而不是"服务器忘了下发"。
        return cfg

    host, port = args.public_ip, args.turn_port
    # STUN 在 turn 模式下不能省。没有它，浏览器只有 host candidate
    # （192.168.x.x，或者 Chrome 的 mDNS 混淆名），公网 VPS 根本不可达，
    # TURN 中继也就无从建立 permission、发不过去。
    cfg["iceServers"].append({"urls": ["stun:%s:%d" % (host, port)]})
    cfg["stunUriForGst"] = "stun://%s:%d" % (host, port)

    if args.ice_mode == "stun":
        return cfg

    username = "%d:%s" % (int(time.time()) + args.ice_ttl, args.room)
    digest = hmac.new(
        args.turn_secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1
    ).digest()
    # 必须是 raw digest 再 base64，**不是 hexdigest** —— 这是 TURN REST
    # 最常见的失败点，而且失败时只是 401，看不出是编码错了。
    credential = base64.b64encode(digest).decode("ascii")

    cfg["iceServers"].append({
        "urls": [
            "turn:%s:%d?transport=udp" % (host, port),
            "turn:%s:%d?transport=tcp" % (host, port),   # UDP 被墙时兜底
        ],
        "username": username,
        "credential": credential,
    })

    # 下面这个是给 GStreamer webrtcbin 用的。浏览器写的是 stun:host:port，
    # webrtcbin 要的是 stun://host:port，格式不一样，统一在这里生成好，
    # 客户端拿到直接 set_property，不做任何拼接或转换。
    #
    # turnUriForGst 里的 user 和 credential 都做了 percent-encode。
    # 这不是洁癖：webrtcbin 解析 turn:// 时**只按第一个 ':' 切分 userinfo**，
    # 而上面的 username 天生就含一个 ':'。不转义会被解析成
    # user="1758600000"、pass="demo:xxxx"，认证必然 401，而且只在日志里留一条
    # 很容易忽略的 WARNING（GStreamer 1.20.3 本地实测复现过）：
    #     WARN gstwebrtcice.c:269 _parse_userinfo: userinfo ... contains more
    #          than one ':', will assume that the first ':' delineates user:pass.
    # 所以这里生成好**完整可用**的 URI 再下发，转义逻辑全项目只此一处。
    cfg["turnUriForGst"] = "turn://%s:%s@%s:%d" % (
        quote(username, safe=""), quote(credential, safe=""), host, port,
    )
    return cfg


def ice_config(args, force=False):
    now = time.monotonic()
    if force or _ice_cache["cfg"] is None or now - _ice_cache["at"] > _ICE_CACHE_SECONDS:
        _ice_cache["cfg"] = build_ice_config(args)
        _ice_cache["at"] = now
    return _ice_cache["cfg"]


# --------------------------------------------------------------------------- #
# 连接封装：串行化发送
# --------------------------------------------------------------------------- #
class Conn:
    """给一条 WebSocket 连接套一层发送锁。

    aiohttp 的 WebSocketWriter **没有发送锁**。这个服务器里，**发布端那条连接
    会被 N 个观看端的转发任务同时写**，两个任务在 await 点交错有概率把帧写坏。
    代价可以忽略，不加则是偶发、难复现的数据损坏。
    """

    def __init__(self, ws, name):
        self.ws = ws
        self.name = name
        self.lock = asyncio.Lock()
        self.broken = False

    async def send(self, obj):
        if self.broken or self.ws.closed:
            return
        async with self.lock:
            try:
                await self.ws.send_json(obj)
            except (ConnectionResetError, RuntimeError):
                # ws.closed 只在 receive() 读到 CLOSE 帧时才置位，发送侧失败
                # 它是不知道的，所以这里要自己记一笔。
                self.broken = True


# --------------------------------------------------------------------------- #
# 房间
# --------------------------------------------------------------------------- #
class Room:
    """一个房间 = 一个发布端 + N 个观看端。

    单房间（进程启动时用 --room 定死）。多房间只要把这里按名字放进字典即可，
    协议不用动。
    """

    def __init__(self, name):
        self.name = name
        self.publisher = None       # Conn
        self.epoch = 0              # 发布端每次重连 +1，用来作废旧会话
        self.viewers = {}           # vid -> Conn

    def new_viewer_id(self):
        while True:
            vid = secrets.token_hex(3)      # 6 个 hex 字符，够用且能肉眼比对
            if vid not in self.viewers:
                return vid


# --------------------------------------------------------------------------- #
# 发布端（板子）
# --------------------------------------------------------------------------- #
async def handle_publish(request):
    args = request.app["args"]
    room = request.app["room"]

    async def reject(code, message):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "error", "code": code, "message": message})
        await ws.close()
        return ws

    if request.query.get("room") != room.name:
        return await reject("bad-room", "房间号不对，本机房间是 %r" % room.name)
    if request.query.get("token") != args.token:
        return await reject("bad-token", "token 不对（--token）")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    if room.publisher is not None:
        return await reject("room-busy", "这个房间已经有发布端了")

    room.epoch += 1
    epoch = room.epoch
    pub = Conn(ws, "publisher")
    room.publisher = pub
    log.info("发布端已连接: %s (epoch=%d, 当前 %d 个观看端)",
             request.remote, epoch, len(room.viewers))

    await pub.send({"type": "ice-servers", **ice_config(args)})

    # 浏览器可能先到。把已经在等的观看端补发一遍 viewer-joined，
    # 而不是在发布端未到时把消息缓存起来 —— 前者不需要任何缓冲逻辑。
    for vid in list(room.viewers):
        await pub.send({"type": "viewer-joined", "viewer": vid})
    if room.viewers:
        log.info("补发 %d 个已在等待的观看端", len(room.viewers))

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                log.warning("发布端发来非 JSON: %s", msg.data[:200])
                continue

            kind = data.get("type")
            vid = data.get("viewer")
            viewer = room.viewers.get(vid)

            if kind == "bye":
                # 只结束这一个观看端，不断开发布端本身
                if viewer is not None:
                    await viewer.send({"type": "publisher-gone"})
                continue

            if viewer is None:
                log.warning("发布端发来 %s，但 viewer %r 不存在，忽略", kind, vid)
                continue

            if kind == "offer":
                await viewer.send({"type": "offer", "sdp": data["sdp"]})
            elif kind == "candidate":
                await viewer.send({
                    "type": "candidate",
                    "candidate": data.get("candidate"),
                    "sdpMLineIndex": data.get("sdpMLineIndex", 0),
                })
            elif kind == "error":
                await viewer.send(
                    {"type": "error", "message": data.get("message", "发布端错误")}
                )
            else:
                log.debug("忽略发布端消息: %s", kind)
    finally:
        log.info("发布端已断开: %s (epoch=%d)", request.remote, epoch)
        # 只有"当前这一代"的发布端才有资格清空房间。否则旧连接的 finally
        # 会把重连后的新会话一起干掉 —— 表现为重连后浏览器再也收不到画面。
        if room.publisher is pub and room.epoch == epoch:
            room.publisher = None
            for viewer in list(room.viewers.values()):
                await viewer.send({"type": "publisher-gone"})
    return ws


# --------------------------------------------------------------------------- #
# 观看端（浏览器）
# --------------------------------------------------------------------------- #
async def handle_view(request):
    room = request.app["room"]

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    if request.query.get("room", "default") != room.name:
        await ws.send_json({"type": "error", "code": "bad-room",
                            "message": "房间号不对，本机房间是 %r" % room.name})
        await ws.close()
        return ws

    vid = room.new_viewer_id()
    viewer = Conn(ws, "viewer-%s" % vid)
    room.viewers[vid] = viewer
    log.info("观看端 %s 已连接: %s (当前 %d 个)", vid, request.remote, len(room.viewers))

    pub = room.publisher
    if pub is None:
        await viewer.send({"type": "waiting", "message": "发布端还没连上，等它进来…"})
    else:
        await pub.send({"type": "viewer-joined", "viewer": vid})

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            pub = room.publisher
            if pub is None:
                continue          # 发布端不在，没什么可转发的

            kind = data.get("type")
            if kind == "answer":
                await pub.send({"type": "answer", "viewer": vid, "sdp": data["sdp"]})
            elif kind == "candidate":
                await pub.send({
                    "type": "candidate", "viewer": vid,
                    "candidate": data.get("candidate"),
                    "sdpMLineIndex": data.get("sdpMLineIndex", 0),
                })
            elif kind == "bye":
                # 观看端说"不看了"。直接跳出循环走 finally 的清理路径
                # （摘掉 viewer + 通知发布端 viewer-left + 关连接），
                # 而不是只把 bye 转发过去 —— 那样服务器这边还留着这条记录，
                # 发布端会继续白白编码一路没人看的流。
                log.info("观看端 %s 主动结束", vid)
                break
            else:
                log.debug("忽略观看端消息: %s", kind)
    finally:
        room.viewers.pop(vid, None)
        log.info("观看端 %s 已断开 (剩 %d 个)", vid, len(room.viewers))
        pub = room.publisher
        if pub is not None:
            await pub.send({"type": "viewer-left", "viewer": vid})
    return ws


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def handle_index(_request):
    return web.Response(
        text=(web_dir() / "monitor.html").read_text(encoding="utf-8"),
        content_type="text/html",
    )


async def handle_ice(request):
    return web.json_response(ice_config(request.app["args"]))


async def handle_health(request):
    room = request.app["room"]
    return web.json_response({
        "ok": True,
        "room": room.name,
        "publisher_online": room.publisher is not None,
        "viewers": len(room.viewers),
    })


# 诊断样本：浏览器和板子各自 POST 上来，留在这里等分析。
#
# **这是无头验证的唯一可行通道。** headless 浏览器里没有人眼，console.log 也拿不到
# （进程是被强杀的），所以只能让页面自己把 getStats() 摘要送回服务器。样本留在
# 服务器上，浏览器被杀掉之后仍然可以慢慢分析。
#
# maxlen 是硬保护：页面万一卡在循环里狂刷，也吃不爆内存。
_diag = collections.deque(maxlen=2000)


async def handle_diag(request):
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            raise web.HTTPBadRequest(text="需要 JSON body")
        body["_at"] = round(time.time(), 3)
        _diag.append(body)
        # 只打一行摘要，别把整份 getStats 刷进日志
        log.debug("[diag %s] %s", body.get("role"),
                  json.dumps(body, ensure_ascii=False)[:300])
        return web.json_response({"ok": True, "n": len(_diag)})

    room = request.query.get("room")
    role = request.query.get("role")
    items = [b for b in _diag
             if (room is None or b.get("room") == room)
             and (role is None or b.get("role", "browser") == role)]
    return web.json_response({"items": items})


async def handle_diag_clear(_request):
    """清空样本。跑下一轮验证前先调一次，免得跨轮次的样本混在一起。"""
    n = len(_diag)
    _diag.clear()
    return web.json_response({"ok": True, "cleared": n})


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def guess_public_ip():
    """取默认路由对应的本机 IP。在腾讯云上这会是**内网 IP**，
    所以只当兜底，真正的公网 IP 要用 --public-ip 明确给。"""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def parse_args():
    p = argparse.ArgumentParser(
        description="WebRTC 信令服务器（远程遥操作）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="0.0.0.0", help="监听地址")
    p.add_argument("--port", type=int, default=8080,
                   help="监听端口。公网部署时避开 80/443：大陆节点未备案会被拦")
    p.add_argument("--room", default="demo", help="房间名")
    p.add_argument("--token", default="demo",
                   help="发布端鉴权用。浏览器侧不需要（连接即身份）")

    i = p.add_argument_group("ICE")
    i.add_argument("--ice-mode", default="none", choices=["none", "stun", "turn"],
                   help="none = 只发空配置（局域网）；stun = 只发 STUN；"
                        "turn = STUN+TURN（公网，需要 --turn-secret）")
    i.add_argument("--public-ip", default=None,
                   help="下发给两端的 STUN/TURN 地址。公网部署必填")
    i.add_argument("--turn-port", type=int, default=3478, help="coturn 端口")
    i.add_argument("--turn-secret", default=None,
                   help="coturn static-auth-secret，用来签 TURN 凭证")
    i.add_argument("--ice-ttl", type=int, default=86400,
                   help="TURN 凭证有效期(秒)。给足，避免长跑后拿到过期凭证")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def quiet_ws_reset(loop):
    """别让 aiohttp 心跳任务在对端消失后抛的 ConnectionResetError 刷满日志。

    aiohttp 3.8 的 WebSocketWriter.ping() 没有防住"transport 正在关闭"这种情况：
    对端一消失，它就在一个后台任务里抛 ConnectionResetError，而这个异常没人接，
    asyncio 于是把整条 traceback 打到日志里。**功能上无害**（服务器照常工作），
    但每断开一个连接就刷一屏红字 —— 对一个专门用来排查问题的工具来说太误导了，
    很容易让人以为服务器挂了。

    心跳本身要留着：发布端的网络如果静默断掉（没有 FIN），没有心跳就永远
    发现不了，观看端会一直卡在 checking。
    """

    def handler(loop_, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and "closing transport" in str(exc):
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)


async def _serve(args):
    quiet_ws_reset(asyncio.get_running_loop())
    app = web.Application()
    app["args"] = args
    app["room"] = Room(args.room)
    app.router.add_get("/", handle_index)
    app.router.add_get("/monitor.html", handle_index)
    app.router.add_get("/ice.json", handle_ice)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/ws/publish", handle_publish)
    app.router.add_get("/ws/view", handle_view)
    app.router.add_route("*", "/diag", handle_diag)
    app.router.add_post("/diag/clear", handle_diag_clear)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    try:
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        print("启动失败: %s" % exc, file=sys.stderr)
        if exc.errno == errno.EADDRINUSE:
            print("\n  端口 %d 已被占用：\n      ss -tlnp | grep :%d\n"
                  % (args.port, args.port), file=sys.stderr)
        return 1

    scheme = "http"
    ws_scheme = "ws"
    ice_note = {
        "none": "无（局域网，只用 host candidate）",
        "stun": "仅 STUN %s:%d" % (args.public_ip, args.turn_port),
        "turn": "STUN+TURN %s:%d" % (args.public_ip, args.turn_port),
    }[args.ice_mode]

    warn = ""
    if args.ice_mode == "turn" and args.public_ip.startswith(
            ("10.", "172.", "192.168.", "127.")):
        warn = ("\n  !! --public-ip 看起来是内网地址。腾讯云上取到的是 VPC 内网 IP，\n"
                "     必须显式传公网 IP，否则下发的 STUN/TURN 地址客户端根本连不上。\n")

    print(
        f"""
============================================================
  WebRTC 信令服务器已启动
------------------------------------------------------------
  监听           : {args.host}:{args.port}   ({scheme})
  房间           : {args.room}
  发布端 token   : {args.token}
  ICE            : {ice_note}

  观看页面:
    {scheme}://{args.public_ip or "<本机IP>"}:{args.port}/?room={args.room}

  发布端:
    python3 src/device_agent.py --signal {ws_scheme}://127.0.0.1:{args.port}/ws/publish \\
        --room {args.room} --token {args.token}

  自检:
    curl {scheme}://127.0.0.1:{args.port}/healthz
    curl {scheme}://127.0.0.1:{args.port}/ice.json
{warn}
  Ctrl-C 退出
============================================================
""",
        file=sys.stderr,
    )
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
    return 0


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.ice_mode in ("stun", "turn") and args.public_ip is None:
        args.public_ip = guess_public_ip()
    if args.ice_mode == "turn" and args.turn_secret is None:
        # 没给就生成一个，但**要打出来** —— 否则重启一次凭证就全变了，
        # 而客户端那边只会看到 401，很难联想到是服务器换了密钥。
        args.turn_secret = secrets.token_hex(32)
        log.warning("未指定 --turn-secret，已随机生成：%s", args.turn_secret)
        log.warning("重启会换密钥。生产环境请固定写在 systemd 单元里")

    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
