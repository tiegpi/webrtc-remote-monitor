#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_server.py —— 板子侧的观看页服务

浏览器只跟板子说话；信令那几个路径由板子转给 VPS 上的信令服务器。
**媒体（RTP）不经过这里** —— 那是浏览器和板子之间 ICE 选出来的直连路径。

    ┌─ 板子 ────────────────────────────┐
    │  device_agent.py ──wss──┐         │        ┌── VPS ──┐
    │                         ├─────────┼───────>│ 信令    │
    │  web_server.py ────wss──┘         │        └─────────┘
    │      ▲                            │
    └──────┼────────────────────────────┘
           │ http / ws（同源）
       浏览器 ──────── RTP 直连 ────────> 板子

为什么信令要绕板子一手，而不是让页面直接连 VPS：

  浏览器直接连 VPS 就得信任 VPS 那张自签证书，而这是**静默失败**的 ——
  fetch() 拿到不受信任的证书直接 reject，WebSocket 连个提示都没有，
  而地址栏那种"点继续前往"的交互在 XHR/WS 上根本不存在。
  让板子去连 VPS（它已经用 --tls-ca 明确信任了那张证书），浏览器这一侧
  就只剩板子一个来源：阶段二给板子加 HTTPS 时，也只需要接受板子一张证书，
  安全上下文和信令一并解决，不用在两台机器上各点一次。

路由：
    /                      monitor.html
    /config.json           本服务的配置（房间号），页面拿它当默认值
    /camera                →  device_agent（本地 unix socket，切换摄像头）
    /stats                 →  device_agent（本地 unix socket，会话/ROS 计数）
    /ice.json  /diag       →  信令服务器（原样转发）
    /ws/view               →  信令服务器（WebSocket 双向透传）
    /healthz               本服务自己的存活检查
    其余                    →  web/ 目录下的静态文件
"""

import argparse
import asyncio
import logging
import ssl
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("web")

HERE = Path(__file__).resolve().parent

# 这些路径原样转给信令服务器。monitor.html 里用的就是这几个相对路径，
# 所以页面代码不需要知道信令服务器到底在哪 —— 同源即可。
PROXY_PATHS = ("/ice.json", "/diag", "/diag/clear")

# 静态兜底路由只放这些后缀。VPS 上是**平铺**部署，web_server.py 和 monitor.html
# 在同一个目录里 —— 不设白名单的话，`GET /web_server.py` 会把源码原样吐出去
# （实测 200）。这里刻意不包含 .py / .pem / .key / .crt / .sock。
STATIC_SUFFIXES = frozenset({
    ".html", ".css", ".js", ".mjs",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".map",
})


def agent_socket_path(args):
    """device_agent 的本地控制端点在哪。

    和 device_agent.socket_path() 是同一件事，刻意**不 import 过来**：
    那个模块导入时就会碰 GStreamer（pipeline 要清 BSP 的环境变量），
    而本服务不需要 GStreamer、也不该被它拖下水 —— 这也是 run-web.sh
    不用 bash -l 的原因。两边靠 --agent-socket 的同一个默认值对齐。
    """
    p = Path(args.agent_socket)
    return p if p.is_absolute() else (HERE.parent / p)


# --------------------------------------------------------------------------- #
def make_ssl_context(args):
    """代理去连 VPS 时用的 SSL 上下文。

    和 device_agent.py 里那个是同一件事、同一个理由：信令服务器用自签证书，
    默认信任库里没有它，必须把那张证书显式当信任锚加载进来。
    不给 --tls-ca 又走 https 时这里返回 None，让默认校验去失败 ——
    那是**对的**行为：证书配错了就该连不上，而不是被悄悄放过。
    """
    if not (args.signal or "").startswith("https://"):
        return None
    return ssl.create_default_context(cafile=args.tls_ca or None)


def web_dir(args):
    """monitor.html 所在目录。

    仓库布局是 src/ + web/ 分开的；VPS 上是**平铺**部署
    （web_server.py 和 monitor.html 在同一个目录）。两种都要能跑：
    脚本旁边有 monitor.html 就用脚本目录，否则按 --web-dir 相对项目根解析。
    和 signal_server.py 里的 web_dir() 是同一个约定，部署脚本不用再传绝对路径。
    """
    d = Path(args.web_dir)
    if d.is_absolute():
        return d
    return HERE if (HERE / "monitor.html").is_file() else (HERE.parent / d).resolve()


def signal_ws_base(args):
    """https://host → wss://host"""
    return args.signal.replace("https://", "wss://").replace("http://", "ws://")


def signal_auth(args):
    """中转去 VPS 时用的 HTTP Basic Auth。

    板子上的 web_server 是浏览器和 VPS 之间的可信中转：浏览器在局域网里
    不需要登录板子，但板子替它去取 `/ice.json`、`/diag`、连 `/ws/view`
    时，VPS 那一侧要求 Basic Auth。凭证只放在板子的 .env 里，不下发给
    局域网浏览器。

    VPS 上的 web_server 代理的是 `127.0.0.1:8443`，那一跳没有 auth_basic
    （鉴权在 nginx），所以它不传 --signal-auth，这里返回 None。
    """
    if not args.signal_auth:
        return None
    user, _, password = args.signal_auth.partition(":")
    return aiohttp.BasicAuth(user, password)


def guess_local_ip():
    """取默认路由对应的本机 IP，只为把可点的地址打在日志里。

    连 8.8.8.8 但**不会真的发包** —— UDP connect 只是让内核选一条路由，
    再把选中的源地址返回。失败就退回 127.0.0.1。
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
# 静态页面
# --------------------------------------------------------------------------- #
async def handle_index(request):
    page = web_dir(request.app["args"]) / "monitor.html"
    if not page.is_file():
        raise web.HTTPNotFound(text="找不到 monitor.html（--web-dir 指对了吗？）")
    # no-store 是必须的：不加的话 aiohttp 只给 Last-Modified/ETag，浏览器按
    # 启发式缓存处理，改了页面之后普通刷新可能根本不回源，表现是"新字段
    # 全是空的"——分不清是没刷、吃了缓存还是代码写错。调试期这个页面每次
    # 都在改，宁可让它每次都重新取。
    return web.FileResponse(page, headers={"Cache-Control": "no-store"})


async def handle_static(request):
    """web/ 目录下的其他文件（将来加 CSS/JS 时用）。"""
    root = web_dir(request.app["args"])
    target = (root / request.match_info["path"]).resolve()
    # 目录穿越防护：resolve() 之后必须仍在 web/ 里面。
    if root not in target.parents and target != root:
        raise web.HTTPForbidden()
    if not target.is_file():
        raise web.HTTPNotFound()
    # 平铺部署时 web_server.py 就在这个目录里，白名单是必须的。
    if target.suffix.lower() not in STATIC_SUFFIXES:
        raise web.HTTPNotFound()
    return web.FileResponse(target)


async def handle_healthz(request):
    args = request.app["args"]
    return web.json_response({
        "ok": True,
        "signal": args.signal,
        "room": args.room,
        "web_dir": str(web_dir(args)),
        "page": (web_dir(args) / "monitor.html").is_file(),
    })


async def handle_config(request):
    """页面的默认配置。目前只有房间号。

    为什么房间号要由板子告诉页面，而不是写死在 monitor.html 里：
    信令服务器只服务它启动时配置的**那一个**房间，房间对不上的连接会被
    `{"type":"error","code":"bad-room"}` 直接关掉。页面里写死一个默认值，
    一旦和 device_agent 的 --room 不一致，症状是"打开页面什么都没发生" ——
    板子日志干干净净，浏览器那边也只是一句连接已断开，两边都不指向真原因。
    （实测踩过：页面默认 demo，板子发的是部署时那个房间号。）

    URL 上的 ?room= 优先级更高，覆盖这里 —— 演示时想临时换个房间不用改代码。
    """
    args = request.app["args"]
    return web.json_response(
        {"room": args.room},
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# 信令转发
# --------------------------------------------------------------------------- #
async def handle_proxy(request):
    """把 /ice.json、/diag 原样转给信令服务器。

    连同请求体、方法、查询串一起转 —— /diag 是 POST JSON，/diag/clear 是 POST，
    /ice.json 是 GET，用同一段代码处理掉，不必按路径分支。
    """
    args = request.app["args"]
    url = args.signal + request.path
    if request.query_string:
        url += "?" + request.query_string

    body = await request.read()
    headers = {}
    if request.headers.get("Content-Type"):
        headers["Content-Type"] = request.headers["Content-Type"]

    try:
        async with request.app["session"].request(
            request.method, url, data=body or None, headers=headers,
            auth=signal_auth(args),
        ) as r:
            payload = await r.read()
            return web.Response(
                body=payload,
                status=r.status,
                content_type=r.content_type or "application/json",
            )
    except aiohttp.ClientError as exc:
        # 信令服务器连不上时，页面那边应该看到明确的错误而不是一个空的 200，
        # 否则 analyze_diag.py 会以为"只是还没有样本"。
        log.warning("转发 %s 失败: %s", request.path, exc)
        raise web.HTTPBadGateway(text="连不上信令服务器 %s: %s" % (args.signal, exc))


async def handle_ws_view(request):
    """把浏览器的 /ws/view 接到信令服务器的 /ws/view，之后双向透传。

    信令是 SDP 和 ICE 候选那样的短消息，透传就够，不需要理解内容 ——
    这样信令协议的改动完全不用碰到这个文件。
    """
    args = request.app["args"]
    target = signal_ws_base(args) + "/ws/view"
    if request.query_string:
        target += "?" + request.query_string

    client = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await client.prepare(request)

    try:
        upstream = await request.app["session"].ws_connect(
            target, heartbeat=30, auth=signal_auth(args))
    except Exception as exc:  # noqa: BLE001
        log.error("连不上信令服务器 %s: %s", target, exc)
        await client.close(code=1011, message=b"signal server unreachable")
        return client

    log.info("观看端已接入，转发到 %s", target)

    async def pump(src, dst, label):
        try:
            async for msg in src:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                else:               # CLOSE / CLOSING / CLOSED / ERROR
                    break
        except (ConnectionResetError, RuntimeError) as exc:
            log.debug("%s 侧断开: %s", label, exc)

    to_up = asyncio.create_task(pump(client, upstream, "浏览器"))
    to_down = asyncio.create_task(pump(upstream, client, "信令"))
    try:
        # 任意一侧断开就收工，剩下的那个任务取消掉。
        await asyncio.wait({to_up, to_down}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (to_up, to_down):
            t.cancel()
        await upstream.close()
        log.info("观看端已断开")

    return client


# --------------------------------------------------------------------------- #
# 摄像头控制：同源转给 device_agent 的 unix socket
# --------------------------------------------------------------------------- #
async def _proxy_agent(request, path, what):
    """把请求原样转给 device_agent 的本地 unix socket。

    走 unix socket 对页面完全透明：它只看到同源的 POST /camera、GET /stats。
    """
    args = request.app["args"]
    sock = agent_socket_path(args)
    body = await request.read()
    headers = {}
    if request.headers.get("Content-Type"):
        headers["Content-Type"] = request.headers["Content-Type"]

    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(str(sock))
        ) as local:
            async with local.request(
                request.method, "http://localhost" + path,
                data=body or None, headers=headers,
            ) as r:
                payload = await r.read()
                return web.Response(
                    body=payload,
                    status=r.status,
                    content_type=r.content_type or "application/json",
                )
    except (aiohttp.ClientError, OSError) as exc:
        # device_agent 没跑（或者 socket 被删了）时给明确的 502。
        # 页面那边会把这句打到日志区 —— 比一个空 200 好得多。
        log.warning("转发 %s（%s）失败（%s）: %s", path, what, sock, exc)
        raise web.HTTPBadGateway(
            text="连不上推送端（%s）。device_agent.py 起了吗？" % sock)


async def handle_camera(request):
    """把 /camera 转给 device_agent 的本地控制端点。

    **为什么让板子中转、而不让页面直连 device_agent**：页面是 https 来的，
    直连会同时踩上这个文件开头总结的那两个坑 —— 跨源，以及自签证书在
    fetch() 里静默 reject（没有任何交互能让你"继续前往"）。绕一手之后，
    浏览器这一侧只剩 web_server 一个来源，和信令那边是同一个思路。
    """
    return await _proxy_agent(request, "/camera", "切换摄像头")


async def handle_stats(request):
    """把 /stats 转给 device_agent。

    只读端点，给 tools/check_ros_mode.sh 用：浏览器那边看动画，脚本这边看
    ros_frames / ros_persons 有没有涨。两者是独立证据，不需要人盯着页面。
    """
    return await _proxy_agent(request, "/stats", "会话统计")


# --------------------------------------------------------------------------- #
async def on_startup(app):
    args = app["args"]
    ctx = make_ssl_context(args)
    if ctx is not None:
        log.info("用 %s 作为信任锚去连信令服务器", args.tls_ca)
    app["session"] = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=ctx)
    )


async def on_cleanup(app):
    await app["session"].close()


def build_app(args):
    app = web.Application()
    app["args"] = args
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/monitor.html", handle_index)
    app.router.add_get("/healthz", handle_healthz)
    # 必须在最后那条 /{path:.*} 兜底路由**之前**注册，否则会被它当静态文件吃掉。
    app.router.add_get("/config.json", handle_config)
    app.router.add_route("*", "/camera", handle_camera)
    app.router.add_get("/stats", handle_stats)
    app.router.add_route("*", "/ws/view", handle_ws_view)
    for p in PROXY_PATHS:
        app.router.add_route("*", p, handle_proxy)
    app.router.add_get("/{path:.*}", handle_static)
    return app


# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="板子侧观看页服务（静态页面 + 把信令转给 VPS）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", type=int, default=8080, help="监听端口")
    p.add_argument("--host", default="0.0.0.0", help="监听地址")
    p.add_argument("--web-dir", default="web", help="页面目录（相对项目根）")
    p.add_argument("--signal", default="http://127.0.0.1:8080",
                   help="信令服务器根地址。信令相关请求都转到这里。"
                        "公网部署时用 run-web.sh 传 https://<VPS>")
    p.add_argument("--signal-auth", default=None, metavar="USER:PASS",
                   help="转发到 VPS 时用的 HTTP Basic Auth。板子中转必须给；"
                        "VPS 本机代理 127.0.0.1:8443 时不需要")
    p.add_argument("--room", default=None,
                   help="房间号，会通过 /config.json 告诉页面。"
                        "**必须和 device_agent.py 的 --room 一致**："
                        "信令服务器只认它启动时配置的那一个房间，"
                        "对不上的连接会被 bad-room 直接关掉")
    p.add_argument("--tls-ca", default=None,
                   help="信令服务器用自签证书时，把那张证书的路径给它")
    p.add_argument("--agent-socket", default="run/agent.sock",
                   help="device_agent.py 本地控制端点的 unix socket（相对路径按"
                        "项目根算），/camera 转给它。**必须和 device_agent.py 的"
                        "--agent-socket 一致**，否则页面上换摄像头的按钮会报 502")
    t = p.add_argument_group("本服务的 TLS（阶段二浏览器要麦克风时才需要）")
    t.add_argument("--tls-cert", default=None, help="证书链 PEM")
    t.add_argument("--tls-key", default=None, help="私钥 PEM")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    scheme = "https" if args.tls_cert else "http"
    ip = args.host if args.host != "0.0.0.0" else guess_local_ip()
    log.info("观看页服务启动")
    log.info("  浏览器开这个: %s://%s:%d/", scheme, ip, args.port)
    log.info("  信令转发到  : %s", args.signal)
    if args.room:
        log.info("  房间号      : %s（已通过 /config.json 告诉页面，不用手写 ?room=）",
                 args.room)
    else:
        log.warning("  房间号      : 没给 --room。页面会退回写死的默认值，"
                    "多半连不上 —— 请在命令行补上，值要和 device_agent.py 的一致")
    log.info("  媒体不经过本服务，是浏览器和板子之间直连的")
    log.info("  摄像头切换  : POST /camera → %s", agent_socket_path(args))
    log.info("  会话统计    : GET /stats   → %s", agent_socket_path(args))
    if args.tls_cert:
        log.info("  浏览器第一次打开会拦自签证书，点一次「继续前往 / 接受风险」即可")

    ssl_ctx = None
    if args.tls_cert:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(args.tls_cert, args.tls_key)
        log.info("已启用 HTTPS（浏览器会需要接受一次自签证书）")

    web.run_app(build_app(args), host=args.host, port=args.port,
                ssl_context=ssl_ctx, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
