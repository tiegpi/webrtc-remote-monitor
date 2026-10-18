#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_textoverlay.py —— 在板子上单独验 textoverlay（**板子上跑**）

    python3 tools/check_textoverlay.py --source mipi --no-wb
    python3 tools/check_textoverlay.py --source mipi --wb
    python3 tools/check_textoverlay.py --source usb  --no-wb

为什么要单独验：阶段一/二从来没有 textoverlay，而阶段三把它插进了
**每一条**采集链路，包括 `--camera-wb` 那条 `appsrc ! caps ! textoverlay`
的新组合。这里要回答三个问题：

1. 文字是空的时侯帧还流不流？—— `wait-text` 默认是 **true**，忘了关就是
   全黑，而且没有任何报错。
2. 改 text 会不会打断码流、帧率掉不掉？—— 旧项目说改 3 次稳定 30fps，
   但那是另一条管线，不能替这里背书。
3. 两路摄像头的像素格式（mipi=UYVY，usb=NV12）都能不能直接协商上？
   textoverlay 的 sink caps 两种都吃，所以不该出现 `videoconvert`；
   真出现了要在这里看见，而不是等浏览器黑屏。

用的是**真实的** `pipeline.VideoSegment`，不是另写一条简化的链 ——
否则测的是测试代码，不是产品代码。
"""

import argparse
import logging
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

# 顺序和 device_agent 一致：pipeline 必须在 Gst.init() 之前导入，
# 它会在导入时清掉板子 BSP 埋的 GST_V4L2_USE_LIBV4L2。
import device_agent  # noqa: E402
import pipeline      # noqa: E402
import sources       # noqa: E402
import teleop        # noqa: E402
from gi.repository import Gst  # noqa: E402

log = logging.getLogger("textoverlay")


def count_buffers(pad, info):
    """只数 buffer，不碰内容。跑在 streaming 线程上，做的事越少越好。"""
    if info.type & Gst.PadProbeType.BUFFER_LIST:
        bl = info.get_buffer_list()
        stat["n"] += bl.length() if bl is not None else 0
    elif info.get_buffer() is not None:
        stat["n"] += 1
    return Gst.PadProbeReturn.OK


stat = {"n": 0}


def drain(bus, errors):
    while True:
        m = bus.pop()
        if m is None:
            return
        if m.type == Gst.MessageType.ERROR:
            err, debug = m.parse_error()
            errors.append("%s（%s）" % (err.message, debug or "无 debug"))
        elif m.type == Gst.MessageType.EOS:
            errors.append("管线提前 EOS")


def measure(pipe, bus, errors, seconds):
    """数一段时间的帧，返回 (fps, 本段帧数)。"""
    drain(bus, errors)
    before = stat["n"]
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        time.sleep(0.1)
        drain(bus, errors)
        if errors:
            break
    span = time.monotonic() - t0
    n = stat["n"] - before
    return (n / span if span > 0 else 0.0), n


def run(args):
    src = sources.resolve(args.source)
    argv = ["--camera-source", src.name]
    # USB 那路是 MJPG，白平衡桥按定义跳过；传了也不会生效，不如不传，
    # 让这里跑的就是页面上切过去时真正的那套配置。
    if args.wb and src.kind == "raw":
        argv += ["--camera-wb", args.wb_value]
    a = device_agent.parse_args(argv)

    Gst.init(None)
    errors = []

    seg = pipeline.VideoSegment(a, src, log)
    pipe = Gst.Pipeline.new("check-textoverlay")
    pipe.add(seg.bin)

    sink = Gst.ElementFactory.make("fakesink", "sink")
    sink.set_property("sync", False)
    # **`async=false` 不能省。** WB 那条路是"两链"结构（appsink 和 appsrc 之间
    # 没有 !），pipeline 要等所有 sink preroll 完才进 PLAYING；fakesink 拿不到
    # 数据（appsrc 空）→ 桥在等 appsink → 源任务在 PLAYING 前不往 appsink 推，
    # 环路闭合，永远卡在 PAUSED。生产管线的末端是 webrtcbin，不参与这种
    # preroll 等待，所以这是测试脚手架的坑，不是设计的坑。
    # 实测：默认 async 时 0 帧；改 false 后 5 秒 150 帧（30fps 一帧不差）。
    sink.set_property("async", False)
    pipe.add(sink)
    sink_pad = sink.get_static_pad("sink")
    if seg.pad.link(sink_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError("采集段接不上 fakesink")

    sink_pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                       count_buffers)

    bus = pipe.get_bus()
    ret = pipe.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("pipeline 起不来（看上面的 GStreamer 报错）")
    seg.start_bridge()

    fails = []
    try:
        log.info("预热 %.1fs（ISP/编码器/白平衡都还没稳）…", args.warmup)
        time.sleep(args.warmup)
        drain(bus, errors)

        # 1) 文字为空时帧必须照常流。wait-text=true 的话这里会是 0。
        fps0, n0 = measure(pipe, bus, errors, args.phase)
        log.info("空文字：    %.1f fps（%d 帧 / %.1fs）", fps0, n0, args.phase)
        if n0 == 0:
            fails.append("文字为空时一帧都没有 —— textoverlay 的 wait-text 没关掉")

        caps = sink_pad.get_current_caps()
        log.info("协商结果：  %s", caps.to_string() if caps else "（没有 caps）")
        if caps is None:
            fails.append("sink pad 上没有 caps，协商没完成")

        # 2) 设文字，帧率不能塌；属性要真的写进去。
        overlay = seg.overlay
        text = teleop.overlay_text(1, "FORWARD", 12, "14:32:05.1")
        overlay.set_property("text", text)
        got = overlay.get_property("text")
        log.info("设置文字：  %r", got)
        if got != text:
            fails.append("text 属性没写进去：%r != %r" % (got, text))

        fps1, n1 = measure(pipe, bus, errors, args.phase)
        log.info("有文字：    %.1f fps（%d 帧 / %.1fs）", fps1, n1, args.phase)
        if n1 == 0:
            fails.append("设置文字后一帧都没有 —— 叠加过程把码流卡死了")
        elif fps0 > 0 and fps1 < fps0 * args.min_ratio:
            fails.append("设置文字后帧率从 %.1f 掉到 %.1f（下限是空文字时的 %.0f%%）"
                         % (fps0, fps1, args.min_ratio * 100))

        # 3) 清空，确认还能继续出帧（2 秒后清空的逻辑就是这么做的）。
        overlay.set_property("text", "")
        fps2, n2 = measure(pipe, bus, errors, args.phase)
        log.info("清空后：    %.1f fps（%d 帧 / %.1fs）", fps2, n2, args.phase)
        if n2 == 0:
            fails.append("清空文字后一帧都没有")
    finally:
        pipe.set_state(Gst.State.NULL)
        pipe.get_state(2 * Gst.SECOND)
        seg.teardown(pipe, sink_pad, log)

    if errors:
        for e in errors:
            log.error("GStreamer: %s", e)
        fails.append("管线报了 %d 个错误" % len(errors))

    log.info("-" * 60)
    if fails:
        for f in fails:
            log.error("失败：%s", f)
        return 1
    log.info("通过：空文字有帧、有文字不掉帧、清空后继续出帧")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="验证 textoverlay 在真实采集段上的协商与帧率（板子上跑）")
    p.add_argument("--source", default="mipi", choices=sources.names())
    p.add_argument("--wb", dest="wb", action="store_true",
                   help="给 raw 源开白平衡桥，走 appsrc ! caps ! textoverlay 这条新组合")
    p.add_argument("--no-wb", dest="wb", action="store_false",
                   help="不开白平衡桥（默认）。写成显式选项是为了让三种组合的"
                        "命令行在验收记录里一眼能区分")
    p.add_argument("--wb-value", default="auto",
                   help="--camera-wb 的值：auto 或 'R,B'")
    p.add_argument("--warmup", type=float, default=3.0,
                   help="预热秒数。白平衡自校准要前 15 帧，别省")
    p.add_argument("--phase", type=float, default=2.5,
                   help="每个测量阶段持续多少秒")
    p.add_argument("--min-ratio", type=float, default=0.7,
                   help="有文字时的帧率下限，相对空文字的比例")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if pipeline.LIBV4L2_WAS_FORCED:
        log.info("已清掉 BSP 导出的 GST_V4L2_USE_LIBV4L2")

    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
