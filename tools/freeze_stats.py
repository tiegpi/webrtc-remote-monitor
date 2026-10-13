#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
freeze_stats.py —— 冻结统计：把"画面卡不卡"变成一个数

analyze_diag.py 只报"最长冻结"，用来做通过/不通过。这个脚本是它的放大镜：
调参（比如 gop）前后各跑一次，比较冻结**次数**、**总时长占比**和**丢包**的关系。

为什么要单独一个脚本：平均帧率会把间歇性冻结摊平。实测 gop=60 时稳态帧率
17.6fps 看着还行，实际有 7% 的时间画面是**完全静止**的 —— 这两件事对观感的
影响差得远，靠一个平均值分不出来。

用法：
    python3 tools/freeze_stats.py --url https://1.2.3.4 --room demo --cafile vps.crt
    python3 tools/freeze_stats.py --file samples.json
"""

import argparse
import json
import sys
from collections import namedtuple

from analyze_diag import (diag_url, last_time_run, live_runs, load_samples,
                          split, ssl_context, steady)

Stats = namedtuple("Stats", "span events total worst loss fps n")


def analyse(win):
    """把稳态窗口压成几个数。"""
    seq = []
    prev = None
    for s in win:
        if prev is not None:
            dt = s["_at"] - prev["_at"]
            if 0 < dt < 10:
                v, pv = s["video"], prev["video"]
                seq.append({
                    "t": s["_at"],
                    "dt": dt,
                    "df": v["framesDecoded"] - pv["framesDecoded"],
                    "dl": (v.get("packetsLost") or 0) - (pv.get("packetsLost") or 0),
                })
        prev = s

    # 相邻的零帧间隔并成一次冻结事件 —— 一帧没解出来连续 2 秒是**一次**卡顿，
    # 不是两次。分开数会把"卡了多久"高估成"卡了几回"。
    events = []
    for i, x in enumerate(seq):
        if x["df"] <= 0:
            if events and i - events[-1][-1] == 1:
                events[-1][-1] = i
            else:
                events.append([i, i])

    span = win[-1]["_at"] - win[0]["_at"]
    total = sum(x["dt"] for x in seq if x["df"] <= 0)
    frames = win[-1]["video"]["framesDecoded"] - win[0]["video"]["framesDecoded"]
    return Stats(
        span=span,
        events=len(events),
        total=total,
        worst=max((sum(seq[i]["dt"] for i in range(a, b + 1))
                   for a, b in events), default=0.0),
        loss=sum(x["dl"] for x in seq),
        fps=frames / span if span > 0 else 0,
        n=len(win),
    ), seq, events


def report(tag, st, seq, events):
    print("【%s】窗口 %.1fs / %d 条" % (tag, st.span, st.n))
    print("    冻结 %d 次，合计 %.1fs —— 占 %.1f%% 的时间画面是停的"
          % (st.events, st.total, 100 * st.total / st.span if st.span else 0))
    print("    单次最长 %.2fs   丢包 %d 个   平均帧率 %.1f fps"
          % (st.worst, st.loss, st.fps))
    # 每次冻结之前 3 秒内有没有丢包 —— 这是"冻结由丢包触发"的判据。
    # 实测 gop=60：7 次冻结，7 次都紧跟在丢包之后。冻结期间丢包是 0，
    # 因为包早就丢完了，不需要再丢新的就能让画面继续冻着。
    triggered = 0
    for a, _b in events:
        if sum(x["dl"] for x in seq[max(0, a - 3):a + 1]) > 0:
            triggered += 1
    print("    其中 %d/%d 次冻结之前 3 秒内有丢包" % (triggered, st.events))
    print()


def main():
    p = argparse.ArgumentParser(description="冻结统计（调参前后对比用）")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--file")
    p.add_argument("--room", default="demo")
    p.add_argument("--cafile", default=None)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--warmup", type=float, default=15.0)
    p.add_argument("--max-gap", type=float, default=5.0)
    args = p.parse_args()

    samples = load_samples(args)
    if not samples:
        print("没有样本。", file=sys.stderr)
        return 2
    browser, device = split(samples)
    _runs, br = live_runs(browser)
    if not br:
        print("浏览器样本没有形成连续会话。", file=sys.stderr)
        return 2
    win = steady(br, args.warmup)
    if len(win) < 3:
        print("稳态窗口只剩 %d 条，再多开一会儿。" % len(win), file=sys.stderr)
        return 2

    st, seq, events = analyse(win)
    report("当前", st, seq, events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
