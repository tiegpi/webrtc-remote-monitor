#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sdpview.py —— 把 SDP 读成人话

阶段二反复要回答同一个问题：「音频那一路到底有没有、协商成什么方向」。
这个判断在板子侧和浏览器侧都要做（tools/check_offer.py 看板子的 offer，
tools/selftest.py 看自己回的 answer），所以解析放在这里共用一份。

**只用标准库，不 import gi / aiohttp** —— 这样它在没有 GStreamer 的机器上
（比如只想看看一段 SDP 文本）也能直接跑。
"""

# 方向属性。SDP 里不写这一行的话默认就是 sendrecv，所以"没写"和"写了
# sendrecv"是一回事，别把它们显示成两种东西。
DIRECTIONS = ("sendrecv", "sendonly", "recvonly", "inactive")

DEFAULT_DIRECTION = "（未写，默认 sendrecv）"


def split_m_sections(sdp):
    """按 m= 行把 SDP 切成若干节，每节带上它自己的属性行。

    一定要按节切：`a=sendrecv` 是**每节各自**的，混在一起看会把视频的方向
    当成音频的方向 —— 而这两路完全可能一个 sendrecv 一个 recvonly。
    """
    sections, cur = [], None
    for raw in (sdp or "").splitlines():
        line = raw.strip()
        if line.startswith("m="):
            cur = {"m": line, "attrs": []}
            sections.append(cur)
        elif cur is not None:
            cur["attrs"].append(line)
    return sections


def media_kind(section):
    """`m=audio 0 UDP/... 111` -> 'audio'。

    第一个字段带 "m=" 前缀，直接拿去和 "audio" 比会永远不等 —— 那正是
    "明明有音频却说没有"的写法。
    """
    fields = section["m"].split()
    if not fields:
        return ""
    return fields[0][2:] if fields[0].startswith("m=") else fields[0]


def describe(section):
    """从一节里取出 (方向, 载荷类型, 该 PT 的编码名列表)。"""
    direction = DEFAULT_DIRECTION
    for a in section["attrs"]:
        if a.startswith("a=") and a[2:] in DIRECTIONS:
            direction = a[2:]
            break

    fields = section["m"].split()
    pt = fields[3] if len(fields) > 3 else "?"

    codecs = []
    for a in section["attrs"]:
        # a=rtpmap:102 H264/90000
        if a.startswith("a=rtpmap:"):
            num, _, name = a.split(":", 1)[1].partition(" ")
            if num == pt:
                codecs.append(name)
    return direction, pt, codecs


def find(sdp, kind):
    """按介质类型找第一节，找不到回 None。"""
    for sec in split_m_sections(sdp):
        if media_kind(sec) == kind:
            return sec
    return None
