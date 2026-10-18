#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""teleop.py —— 遥操作命令的协议层

这一层只管"一句话长什么样、什么算合法"，**不碰 GStreamer、不碰网络**。
这样同一个白名单和同一条 ACK 格式，板子上的 `session.py` 和无头对端
`tools/selftest.py` 用的是同一份代码；协议单测也能在没有 GStreamer 的
宿主机上直接跑（见 tools/check_teleop.py）。

协议（PLAN-phase2-3.md 第 182 行起）：

    浏览器 → 板子   {"t":"cmd","seq":7,"cmd":"FORWARD","arg":12,"ts":1728...}
    板子 → 浏览器   {"t":"ack","seq":7,"cmd":"FORWARD","ts":...,"ms":3.2}
    板子 → 浏览器   {"t":"nack","seq":7,"cmd":"...","ts":...,"reason":"unknown-command"}

**白名单外一律 nack，不静默丢弃。** 静默丢弃在页面上表现为"发了但没反应"，
分不清是命令不合法、DataChannel 断了还是板子没执行 —— 而这三件事要改的地方
完全不同。回执让每一种失败都留下名字。

`on-message` 这个名字是 PLAN 里凭印象写的；GStreamer 1.20.3 实测没有它，
只有 `on-message-string` / `on-message-data`。`connect_message()` 按新名到旧名
依次尝试，别的版本上也不会因为名字不同而静默收不到命令。
"""

import json
import math

#: 允许执行的命令。顺序就是页面快捷按钮的顺序。
COMMANDS = ("FORWARD", "BACK", "LEFT", "RIGHT", "STOP", "SPEED")

#: 一条命令的上限。远大于正常消息（约 60 字节），只用来挡明显异常的东西。
MAX_MESSAGE_BYTES = 4096

ARG_MIN, ARG_MAX = -1000, 1000


class ProtocolError(ValueError):
    """不合法的命令。`reason` 是写进 nack 的稳定字符串，别改已有取值。

    seq/cmd/ts 是**能回显就回显**的字段：页面靠它们把 nack 对回自己发出去的
    那一条。解析都失败了就没有 seq —— 那时是 None，不是 0。
    """

    def __init__(self, reason, seq=None, cmd=None, ts=None, detail=None):
        text = reason if detail is None else "%s: %s" % (reason, detail)
        super().__init__(text)
        self.reason = reason
        self.seq = seq
        self.cmd = cmd
        self.ts = ts
        self.detail = detail


def parse(raw):
    """把 DataChannel 收到的东西变成 Python 对象。

    浏览器发字符串，GStreamer 的 `on-message-string` 给 str；`on-message-data`
    给 GBytes（构造测试或别的对端时也可能是 bytes）。三种都认。
    """
    data = _as_bytes(raw)
    if data is None:
        raise ProtocolError("bad-type", detail="消息不是文本/字节")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("too-large",
                            detail="%d 字节（上限 %d）" % (len(data), MAX_MESSAGE_BYTES))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # 不是合法 UTF-8 的字节，本质上就是坏的 JSON 文本。
        raise ProtocolError("bad-json", detail="不是 UTF-8: %s" % exc) from exc
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("bad-json", detail=str(exc)) from exc


def _as_bytes(raw):
    """str / bytes / bytearray / memoryview / GBytes(GObject) → bytes。"""
    if isinstance(raw, str):
        return raw.encode("utf-8")
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw)
    # PyGObject 的 GBytes 不是 bytes 子类，只暴露 get_data()。鸭子类型判断，
    # 这样模块本身不需要 import gi。
    get_data = getattr(raw, "get_data", None)
    if callable(get_data):
        try:
            data = get_data()
        except Exception:                        # noqa: BLE001
            return None
        if isinstance(data, (bytes, bytearray, memoryview)):
            return bytes(data)
    return None


def validate(msg):
    """校验并**归一化**一条命令。返回只含已知字段的新 dict。

    顺序是刻意定的：先 seq 再 cmd —— 页面最常犯的是漏字段，而 seq 是它
    唯一能用来匹配回执的东西，先确保它可用，后面的错误才回得去。
    """
    if not isinstance(msg, dict):
        raise ProtocolError("bad-type", detail="顶层不是对象")

    seq = msg.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        # bool 是 int 的子类，True 会被当成 1 —— 必须显式排除。
        raise ProtocolError("bad-seq",
                            seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None)

    cmd = msg.get("cmd")
    if not isinstance(cmd, str):
        raise ProtocolError("bad-cmd", seq=seq)

    if msg.get("t") != "cmd":
        raise ProtocolError("bad-type", seq=seq, cmd=cmd,
                            detail="t=%r（只认 \"cmd\"）" % (msg.get("t"),))

    # ts 在查白名单**之前**校验：未知命令也要能在 nack 里回显 ts，
    # 页面才能把它对回自己发出去的那一条。
    ts = msg.get("ts")
    if (isinstance(ts, bool) or not isinstance(ts, (int, float))
            or not math.isfinite(ts) or ts < 0):
        raise ProtocolError("bad-ts", seq=seq, cmd=cmd)

    if cmd not in COMMANDS:
        raise ProtocolError("unknown-command", seq=seq, cmd=cmd, ts=ts,
                            detail="可用：%s" % ", ".join(COMMANDS))

    arg = msg.get("arg")
    if arg is not None:
        if (isinstance(arg, bool) or not isinstance(arg, int)
                or not (ARG_MIN <= arg <= ARG_MAX)):
            raise ProtocolError("bad-arg", seq=seq, cmd=cmd, ts=ts,
                                detail="%r，要求 %d..%d 的整数"
                                       % (arg, ARG_MIN, ARG_MAX))
    elif cmd == "SPEED":
        # 方向命令和 STOP 不带参数有意义（STOP 就是 STOP）；SPEED 不带就没法执行。
        raise ProtocolError("missing-arg", seq=seq, cmd=cmd, ts=ts,
                            detail="SPEED 必须带 arg")

    out = {"t": "cmd", "seq": seq, "cmd": cmd, "ts": ts}
    if arg is not None:
        out["arg"] = arg
    return out


def ack(seq, cmd, ts, ms):
    """处理成功的回执。`ms` 是板端处理耗时，不是 RTT —— RTT 由页面自己量。"""
    return {"t": "ack", "seq": seq, "cmd": cmd, "ts": ts,
            "ms": round(float(ms), 3)}


def nack(seq, cmd, ts, reason):
    return {"t": "nack", "seq": seq, "cmd": cmd, "ts": ts, "reason": reason}


def nack_from(exc):
    """把 ProtocolError 变成回执。能用 `except ProtocolError as exc` 接住。"""
    return nack(exc.seq, exc.cmd, exc.ts, exc.reason)


def overlay_text(seq, cmd, arg=None, when=""):
    """画面左上角那行字。**只用 ASCII**。

    板子上没有中文字体（`fc-list :lang=zh` 为 0），写中文会渲染成方框；
    命令和时间戳本来就只该有可打印 ASCII。
    """
    text = "CMD #%d %s" % (seq, cmd)
    if arg is not None:
        text += " %d" % arg
    if when:
        text += "  " + when
    return text


def connect_message(channel, handler):
    """给 DataChannel 挂消息回调，返回**实际连上**的信号名。

    1.20.3 上只有 on-message-string / on-message-data；PLAN 里写的 on-message
    在真机上不存在。依次试，别用 hasattr 猜 —— GObject 的属性表里没有信号。
    """
    attempts = ("on-message-string", "on-message-data", "on-message")
    last_error = None
    for name in attempts:
        try:
            channel.connect(name, handler)
        except TypeError as exc:
            last_error = exc
            continue
        return name
    raise RuntimeError("DataChannel 上没有可用的消息信号（试过 %s）：%s"
                       % (" / ".join(attempts), last_error))
