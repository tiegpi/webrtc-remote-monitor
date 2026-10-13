#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_force_keyunit.py —— 板子的编码器认不认 GstForceKeyUnit？

为什么需要这个：链路上"丢包之后要等 3 秒"的根因是 gop=60 在 20fps 下 IDR 间隔
太长。缩短 gop 是笨办法（每帧都付画质代价换一个"万一"）。好办法是**按需出关键帧**：
浏览器丢包后发 PLI，板子把 PLI 转成给编码器的 GstForceKeyUnit，编码器立刻插一个 IDR，
代价只在真出问题的时候付。

这条链有两半，任何一半断了整个方案就是空的：
    浏览器 --PLI--> rtpbin --> GstForceKeyUnit --> 编码器插 IDR
                    └── 上半：rtpbin 转不转？
                    └── 下半：编码器认不认？  ← 这个脚本测的就是这一半

下半可以用纯 GStreamer 单独测：不接浏览器、不接 rtpbin，直接往编码器发事件，
数输出里的 NAL 类型（5 = IDR，1 = 非 IDR）。gop=60 的流水线上在第 40 帧发事件，
如果第 41 帧就出 IDR（不在 0/60/120/180 这些 gop 边界上），说明编码器完全听话，
问题只可能出在上半。

两个踩过的坑，写在这儿免得下次再踩：
  1. h264parse 输出**字节流**，每个 NAL 前面有 00 00 00 01 起始码。读 NAL 类型
     要扫起始码取后面那个字节，直接读 data[0] 拿到的是起始码，永远是 0 ——
     表现为"整条流一个 IDR 都没有"，看着像编码器坏了，其实是解析错了。
  2. 触发不能靠定时器。fakesink sync=false 时整条流水线全速跑完（200 帧几毫秒
     就 EOS），2 秒的 timeout 永远等不到。改成**按缓冲计数**触发，并用 sync=true
     让流水线按真实 20fps 走，帧位置才有意义。

用法（在板子上跑）：
    python3 tools/test_force_keyunit.py
"""

import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo

Gst.init(None)

# GstForceKeyUnit 不在 Gst.Event 上，它是 libgstvideo 提供的
# gst_video_event_new_upstream_force_key_unit()。PyGObject 会剥掉命名空间
# 前缀，但具体剥成什么名字随版本而变，所以探测一下而不是写死。
# （写死的代价实测过：构造函数抛 AttributeError，事件根本没发出去，而
#  GLib.idle_add 把异常吞进 stderr，脚本照样跑完并报出"编码器不认"——
#  一个没做的实验变成了一个错误结论。）
def _fku_maker():
    for name in ("event_new_upstream_force_key_unit",
                 "video_event_new_upstream_force_key_unit"):
        fn = getattr(GstVideo, name, None)
        if fn is not None:
            return name, fn
    return None, None


_FKU_NAME, _FKU_FN = _fku_maker()

N_FRAMES = 200
GOP = 60            # 故意 60：编码器不认事件的话，IDR 只会出现在 0/60/120/180
FPS = 20
FORCE_AT = 40       # 离最近的 gop 边界（60）还差 20 帧 = 1 秒，够区分


def nal_types(data):
    """扫字节流，返回这个缓冲里所有 NAL 的类型。"""
    out, i, n = [], 0, len(data)
    while i + 3 < n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            out.append(data[i + 3] & 0x1F)
            i += 4
        else:
            i += 1
    return out


def build(with_force):
    desc = (
        f"videotestsrc is-live=false num-buffers={N_FRAMES} ! "
        "videoconvert ! video/x-raw,format=NV12,width=1280,height=720,"
        f"framerate={FPS}/1 ! "
        f"mpph264enc gop={GOP} profile=baseline level=31 ! "
        "h264parse ! fakesink sync=true"   # sync=true：按真实帧率走，见上面坑 2
    )
    pipe = Gst.parse_launch(desc)
    parse = _find(pipe, "h264parse")
    nals = []
    state = {"sent": "（没到触发点）", "fired": False}

    def on_src(_pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            nals.append((len(nals), nal_types(bytes(mi.data[:256]))))
            buf.unmap(mi)

        if with_force and not state["fired"] and len(nals) >= FORCE_AT:
            state["fired"] = True
            # 从探针里直接发上游事件有拿锁的风险，丢给主循环去做。
            GLib.idle_add(fire, parse, state)
        return Gst.PadProbeReturn.OK

    parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_src)
    return pipe, nals, state


def fire(parse, state):
    """发一次 GstForceKeyUnit。它是**上游**事件，要从 h264parse 的 sink pad
    发出去 —— 那个 pad 的 peer 是编码器的 src pad，上游事件顺着进编码器。
    从 src pad 发是下游方向，进不了编码器。

    每一步失败都必须记进 state 并传到结论里。上一版就是这里崩了却没人管：
    GLib.idle_add 把 AttributeError 吞进 stderr，主循环照跑，脚本最后报出
    "结论：编码器不认" —— 而事件根本没发出去。一个没做的实验变成了错误结论。
    """
    try:
        if _FKU_FN is None:
            raise RuntimeError("GstVideo 里找不到 force-key-unit 构造函数")
        ev = _FKU_FN(Gst.CLOCK_TIME_NONE, True, 1)
        pad = parse.get_static_pad("sink")
        ok = pad.send_event(ev) if pad else False
        state["sent"] = ("ok:" + _FKU_NAME) if ok else "发送被拒"
    except Exception as exc:                      # noqa: BLE001 —— 故意兜住一切
        state["sent"] = "异常 %s: %s" % (type(exc).__name__, exc)
    print("    >> ForceKeyUnit 发送结果：%s" % state["sent"], flush=True)
    return False


def _find(pipe, factory):
    it = pipe.iterate_elements()
    while True:
        ok, el = it.next()
        if ok != Gst.IteratorResult.OK:
            break
        f = el.get_factory()
        if f and f.get_name() == factory:
            return el
    return None


def run(with_force):
    pipe, nals, state = build(with_force)
    loop = GLib.MainLoop()
    pipe.set_state(Gst.State.PLAYING)
    bus = pipe.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
            if msg.type == Gst.MessageType.ERROR:
                err, _dbg = msg.parse_error()
                print("  流水线报错: %s" % err.message)
            loop.quit()

    bus.connect("message", on_msg)
    GLib.timeout_add_seconds(60, lambda: (loop.quit(), False)[1])
    loop.run()
    pipe.set_state(Gst.State.NULL)

    idrs = [i for i, ts in nals if 5 in ts]
    seen = sorted(set(t for _i, ts in nals for t in ts))
    return idrs, seen, state, len(nals)


def main():
    print("=" * 62)
    print("对照：不发事件，gop=60 的自然 IDR 位置")
    idrs, seen, _s, n = run(False)
    print("  共 %d 个缓冲；IDR 出现在 %s" % (n, idrs))
    print("  出现过的 NAL 类型 %s（1=非IDR片 5=IDR 6=SEI 7=SPS 8=PPS）" % seen)
    natural = idrs
    if not natural:
        print()
        print("对照组一个 IDR 都没有 —— 这不是编码器的问题，是解析或流水线的问题，"
              "先修脚本再谈结论。")
        return 2

    print()
    print("=" * 62)
    print("实验：第 %d 帧附近发一次 ForceKeyUnit" % FORCE_AT)
    idrs2, seen2, state, n2 = run(True)
    print("  共 %d 个缓冲；IDR 出现在 %s" % (n2, idrs2))

    extra = [i for i in idrs2 if i not in natural]
    print()
    print("=" * 62)

    # 先确认刺激真的发出去了，否则后面无论看到什么都不能说编码器的事。
    # 这条闸门就是为了拦住"没做的实验得出错误结论"。
    if not state["sent"].startswith("ok:"):
        print("实验无效：ForceKeyUnit **没有成功发出**（%s）。" % state["sent"])
        print("      IDR 出现在 %s，但这不能解读为编码器不认事件 —— 事件压根没到。" % idrs2)
        return 3

    if not extra:
        print("结论：编码器**不认** GstForceKeyUnit（事件已确认发出：%s）。" % state["sent"])
        print("      IDR 仍然只在 gop 边界 %s 上。" % natural)
        print("  => 按需关键帧这条路在编码器这一半就是断的，只能靠缩短 gop。")
        return 1
    print("结论：编码器**认** GstForceKeyUnit。多出来的 IDR 在 %s，" % extra)
    print("      不在 gop 边界 %s 上，只可能是事件触发的。" % natural)
    print("  => 下半条链是通的。剩下的问题全在 rtpbin 转不转 PLI。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
