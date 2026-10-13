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
from gi.repository import GLib

import camera            # 画质兜底：板子没有 3A，亮度只能自己闭环
import pipeline          # 必须早于 Gst.init()：它会在导入时清掉 BSP 埋的环境变量
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
            }, timeout=aiohttp.ClientTimeout(total=3))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("遥测上报失败（不影响推流）: %s", exc)


# --------------------------------------------------------------------------- #
# 一轮连接
# --------------------------------------------------------------------------- #
async def publish_once(args, url, ice_url):
    """连上信令服务器，按需建会话并推流。

    返回 True 表示**正常断开**（网络抖动、对方关连接），调用方可以重置退避。
    返回 False 表示**被服务器拒绝**（room-busy 之类），要保留退避 ——
    否则退避永远停在 1 秒，变成对服务器每秒一次的锤击。
    """
    quiet_ws_reset(asyncio.get_running_loop())
    clean = True
    loop = asyncio.get_running_loop()
    session = None
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
                telemetry_loop(http, args, lambda: session, http_base_of(url))
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
                        # 单观看端设计：已经有人在看就明确拒绝，
                        # 而不是默默开第二路抢同一个摄像头。
                        if session is not None:
                            log.warning("已有一个观看端，拒绝新的连接")
                            await sender.send({
                                "type": "error",
                                "message": "本设备同时只接受一个观看端",
                            })
                            continue
                        ice = await fetch_ice(http, ice_url)
                        ice.setdefault("stun", "")
                        ice.setdefault("turn", "")
                        ice["stun"] = ice["stun"] or args.stun
                        ice["turn"] = ice["turn"] or args.turn
                        ice["policy"] = args.ice_policy

                        vid = data.get("viewer")
                        if not vid:
                            log.error("服务器发的 viewer-joined 里没有 viewer 字段，"
                                      "无法路由，跳过这次接入")
                            continue
                        session = DeviceSession(
                            tag_viewer(sender, vid), loop, args, ice
                        )
                        try:
                            session.start()
                        except Exception as exc:  # noqa: BLE001
                            log.exception("建 pipeline 失败")
                            await sender.send({"type": "error", "message": str(exc)})
                            session = None
                            continue
                        log.info("观看端已接入，开始推流")

                    elif kind == "viewer-left":
                        if session is not None:
                            session.stop()
                            session = None
                            log.info("观看端已离开")

                    elif kind in ("answer", "candidate"):
                        if session is not None:
                            session.on_signal(data)

                    elif kind == "bye":
                        if session is not None:
                            session.stop()
                            session = None

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
                if session is not None:
                    session.stop()
                    session = None
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


async def fetch_ice(http, ice_url):
    """从信令服务器取 STUN/TURN 配置。取不到就用命令行参数兜底。"""
    if not ice_url:
        return {}
    try:
        async with http.get(ice_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            cfg = await r.json()
        return {
            "stun": cfg.get("stunUriForGst", ""),
            "turn": cfg.get("turnUriForGst", ""),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("取 %s 失败: %s（回退到命令行参数）", ice_url, exc)
        return {}


async def run_publisher(args):
    """主循环：断线自动重连（指数退避）。板子和家宽掉线很常见。"""
    sep = "&" if "?" in args.signal else "?"
    url = "%s%sroom=%s&token=%s" % (
        args.signal, sep, quote(args.room, safe=""), quote(args.token, safe="")
    )
    ice_url = args.ice_url or (http_base_of(args.signal) + "/ice.json")

    log.info("房间 %s，ICE 策略 %s", args.room, args.ice_policy)
    log.info("摄像头 %s  %dx%d@%d  %s  %d kbps",
             args.camera, args.width, args.height, args.fps,
             pipeline.VIDEO_FORMAT, args.bitrate // 1000)
    if pipeline.LIBV4L2_WAS_FORCED:
        log.info("已清掉 GST_V4L2_USE_LIBV4L2（BSP 在登录 shell 里导出它，"
                 "留着会让 rkisp 分配缓冲失败）")

    delay = 1.0
    while True:
        clean = False
        try:
            clean = await publish_once(args, url, ice_url)
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
    g.add_argument("--tls-ca", default=None,
                   help="wss:// 且服务器用自签证书时，把那张证书的路径给它")
    g.add_argument("--stun", default="", help="直接指定 STUN，覆盖服务器下发的")
    g.add_argument("--turn", default="", help="直接指定 TURN，覆盖服务器下发的")
    g.add_argument("--ice-policy", default="all", choices=["all", "relay"],
                   help="relay = 强制走 TURN。用来验证中继真的生效")

    c = p.add_argument_group("摄像头")
    c.add_argument("--camera", default="/dev/video11",
                   help="V4L2 设备节点。MIPI ISP 是 /dev/video11，"
                        "/dev/video0 是裸 Bayer 节点，别用")
    c.add_argument("--width", type=int, default=1280)
    c.add_argument("--height", type=int, default=720)
    c.add_argument("--fps", type=int, default=30)
    c.add_argument("--bitrate", type=int, default=2500000,
                   help="H.264 目标码率(bps)。mpph264enc 的默认值约 3.4Mbps")
    c.add_argument("--gop", type=int, default=60,
                   help="关键帧间隔(帧)。60 = 2 秒。别设太大："
                        "中途加入的浏览器要等到下一个 IDR 才有画面")

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

    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

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

    # 画质兜底放在最前面：此刻还没有观看端、pipeline 还没建，摄像头是空的，
    # 量亮度的那个 ISP 节点不会和正在推的流抢资源。
    camera.setup(args)

    from gi.repository import Gst

    # Gst.init() 必须在这里 —— pipeline 模块已经在导入时清掉了那个环境变量，
    # 而 v4l2 插件是在创建 element 时读它的。
    Gst.init(None)

    # GLib.MainLoop 跑在守护线程上，asyncio 跑在主线程。
    # GStreamer 的回调全部落在 glib 线程，跨线程投递见 session.py 的说明。
    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, name="glib-main", daemon=True).start()

    try:
        return asyncio.run(run_publisher(args))
    except KeyboardInterrupt:
        return 0
    finally:
        glib_loop.quit()


if __name__ == "__main__":
    sys.exit(main())
