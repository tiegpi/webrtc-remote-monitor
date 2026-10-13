#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/check_offer.py —— 不开浏览器，直接看板子发出的 offer 长什么样

阶段二最常问的一个问题是「音频那一路到底有没有、协商成什么方向」。用浏览器
查要开页面、点对讲、翻 chrome://webrtc-internals，一圈下来才能看到一行 SDP；
而这个脚本在板子上跑十几秒就把 m-line、方向、编码都打出来，还能用退出码判成败。

它走的是**真的**那条路：同样的 parse_args、同样的 audio.resolve、同样的
DeviceSession，只是把信令换成一个内存里的假 send_fn —— 所以它不是"另写一遍
描述串"那种会和产品代码跑偏的测试。

    板子上（必须登录 shell，BSP 的 GStreamer 环境变量在那儿）：
        bash -lc 'cd ~/webrtc-remote-monitor && python3 tools/check_offer.py'

    --expect-audio   没有音频 m-line 就非零退出，给脚本/CI 用
    --no-camera-wb   不挂白平衡桥（快一点，图像会偏绿，但 offer 不受影响）

注意它会**真的占用摄像头和麦克风**，跑之前先把正在推流的 device_agent 停掉。

这个脚本不调用 camera.setup()：它写的是 sensor 亮度控件，对 offer 一点影响
都没有，却要花好几秒做闭环测量，而且会和别的东西抢 ISP 节点。
"""

import argparse
import asyncio
import logging
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

# 顺序和 device_agent 保持一致：pipeline 必须在 Gst.init() 之前导入，
# 它会在导入时清掉板子 BSP 埋的 GST_V4L2_USE_LIBV4L2。
import device_agent  # noqa: E402
import audio         # noqa: E402
import pipeline      # noqa: E402
from session import DeviceSession  # noqa: E402
from sdpview import split_m_sections, describe, media_kind  # noqa: E402
from gi.repository import GLib     # noqa: E402

log = logging.getLogger("checkoffer")

# 和 tools/run-agent.sh 保持一致，这样测的就是真正会跑的那套配置。
# 命令行上传的同名参数会覆盖这里（argparse 后出现的赢）。
BASE_ARGV = ["--gop", "10", "--camera-wb", "auto"]


async def amain(args, timeout, expect_audio):
    loop = asyncio.get_running_loop()
    got = {"offer": None, "cands": 0}

    async def send_fn(obj):
        # 这里就是 DeviceSession 往外发信令的地方。真实运行时 send_fn 是往
        # WebSocket 写；这里只在内存里记下来，所以整个脚本不需要信令服务器、
        # 不需要 token、板子上也不用开任何端口。
        kind = obj.get("type")
        if kind == "offer":
            got["offer"] = obj["sdp"]
        elif kind == "candidate":
            got["cands"] += 1

    sess = DeviceSession(send_fn, loop, args, ice={})
    sess.start()

    deadline = loop.time() + timeout
    while got["offer"] is None and loop.time() < deadline:
        await asyncio.sleep(0.2)

    if got["offer"] is None:
        log.error("等了 %.0f 秒还没等到 offer —— 看上面有没有 GStreamer 的错误", timeout)
        sess.stop()
        return 1

    # 再多等一会儿收集候选：ICE 打洞能不能通不在这个脚本的判据里，但候选数是
    # "STUN/TURN 配没配"最直接的观测点，顺便报出来。
    settle = min(3.0, max(0.0, deadline - loop.time()))
    if settle:
        await asyncio.sleep(settle)

    rc = report(got, expect_audio)
    log.info("板端统计: %s", sess.counters())
    sess.stop()
    return rc


def report(got, expect_audio):
    sections = split_m_sections(got["offer"])
    log.info("=" * 66)
    log.info("offer 共 %d 节，ICE 候选 %d 条", len(sections), got["cands"])
    log.info("=" * 66)

    found_audio = False
    for i, sec in enumerate(sections):
        direction, pt, codecs = describe(sec)
        kind = media_kind(sec)
        log.info("[%d] %s", i, sec["m"])
        log.info("      方向 = %s   PT = %s   编码 = %s",
                 direction, pt, ", ".join(codecs) or "（没写 rtpmap）")

        if kind == "audio":
            found_audio = True
            # 这三件事任意一件不对，浏览器那边都是"没声音"，
            # 但原因和要改的地方完全不同，所以分开判、分开说。
            if not any(c.lower().startswith("opus/") for c in codecs):
                log.error("      !! 音频这节的 PT %s 不是 opus。"
                          "板子侧 rtpopuspay 的 pt= 和 caps 里的 payload= 对不上？", pt)
            elif pt != "111":
                log.warning("      ! PT=%s 不是 111。能用，但 Chrome/Firefox 的默认"
                            " offer 里 Opus 是 111，对不上时排查会多一层。", pt)
            if direction == "recvonly":
                log.error("      !! recvonly = 板子只想收。浏览器会听不到板子的声音。"
                          "检查 audio_branch() 有没有真的接到 webrtcbin 的 sink_1")
            elif direction == "inactive":
                log.error("      !! inactive = 这一路被关掉了")

    log.info("-" * 66)
    if found_audio:
        log.info("音频 m-line 在。**但方向是 sendrecv 只说明板子愿意收也愿意发**；"
                 "浏览器的麦克风能不能上来，还要看页面的 addTrack 有没有在 "
                 "setRemoteDescription 之前做完（那一步在浏览器侧，这里测不到）。")
        return 0

    if expect_audio:
        log.error("没有音频 m-line，而 --expect-audio 要求必须有。")
        log.error("板子上常见的两个原因：")
        log.error("  1) 启动日志里报了 AudioUnavailable（3.5mm 插的是三极耳机，"
                  "又没有 USB 采集卡）→ 跑 tools/check_audio.sh 看硬件")
        log.error("  2) 传了 --no-audio")
        return 1
    log.warning("没有音频 m-line（板子在做纯视频，阶段一的行为）。")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="不起浏览器，直接打印板子 offer 的 m-line 和方向",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--timeout", type=float, default=25.0,
                   help="等 offer 的秒数")
    p.add_argument("--expect-audio", action="store_true",
                   help="没有音频 m-line 就非零退出")
    p.add_argument("--no-camera-wb", action="store_true",
                   help="不挂白平衡桥，省几秒")
    p.add_argument("-v", "--verbose", action="store_true")
    known, rest = p.parse_known_args(argv)

    # 剩下的参数原样交给 device_agent.parse_args —— 这样 --gop / --bitrate /
    # --mic-device 这些不用在这里再抄一遍，也就不会抄漏、抄错。
    base = list(BASE_ARGV)
    if known.no_camera_wb:
        base = ["--gop", "10"]
    args = device_agent.parse_args(base + rest)

    logging.basicConfig(
        level=logging.DEBUG if (known.verbose or args.verbose) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.no_audio:
        try:
            audio.resolve(args, log)
        except audio.AudioUnavailable as exc:
            log.error("%s", exc)
            return 2
    else:
        log.info("--no-audio：只测视频，阶段一的行为")

    # 顺带报一下这个：它是"ssh 直接跑正常、登进去手动跑就炸"那个坑的根因。
    # 从非登录 shell 跑时这个变量本来就不存在，所以不打印任何东西。
    if pipeline.LIBV4L2_WAS_FORCED:
        log.info("已清掉 BSP 导出的 GST_V4L2_USE_LIBV4L2"
                 "（登录 shell 会设它，留着的话 rkisp 分配缓冲必失败）")

    from gi.repository import Gst
    Gst.init(None)      # pipeline 模块在导入时已经清掉了那个环境变量

    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, name="glib-main", daemon=True).start()
    try:
        return asyncio.run(amain(args, known.timeout, known.expect_audio))
    except KeyboardInterrupt:
        return 130
    finally:
        glib_loop.quit()


if __name__ == "__main__":
    sys.exit(main())
