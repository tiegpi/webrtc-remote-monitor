#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_teleop.py —— 遥操作协议层的单测（宿主机就能跑，不需要 GStreamer）

    python3 tools/check_teleop.py

覆盖的是"什么算合法、什么必须 nack"：六条白名单命令、每一种 nack reason、
bytes/GBytes 输入、bool/NaN/Infinity、超长消息、SPEED 缺参数、未知命令。
协议一旦悄悄放宽，页面和板子两边都不会报错，只会"命令发了没反应" ——
这些用例就是为了让那种变化在这里先红。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import teleop  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILS.append(name)
        print("FAIL  %-38s %s" % (name, exc))
    except Exception as exc:                     # noqa: BLE001
        FAILS.append(name)
        print("FAIL  %-38s 未预期的异常：%r" % (name, exc))
    else:
        print("PASS  %s" % name)


class FakeGBytes:
    """PyGObject 的 GBytes 不是 bytes 子类，只暴露 get_data()。"""

    def __init__(self, data):
        self._data = data

    def get_data(self):
        return self._data


class FakeChannel:
    """只实现 connect：模拟不同 GStreamer 版本上存在哪些消息信号。"""

    def __init__(self, available):
        self.available = set(available)
        self.connected = []

    def connect(self, name, handler):
        if name not in self.available:
            raise TypeError("signal '%s' is invalid" % name)
        self.connected.append(name)
        return len(self.connected)


def expect_reason(reason, raw=None, msg=None):
    try:
        if raw is not None:
            teleop.validate(teleop.parse(raw))
        else:
            teleop.validate(msg)
    except teleop.ProtocolError as exc:
        assert exc.reason == reason, "期望 reason=%r，实际 %r（%s）" % (
            reason, exc.reason, exc)
        return exc
    raise AssertionError("没有抛 ProtocolError（期望 reason=%r）" % reason)


def msg(**kw):
    base = {"t": "cmd", "seq": 1, "cmd": "FORWARD", "ts": 1728000000000.0}
    base.update(kw)
    return base


def test_parse_inputs():
    assert teleop.parse('{"t":"cmd"}') == {"t": "cmd"}
    assert teleop.parse(b'{"n":1}') == {"n": 1}
    assert teleop.parse(bytearray(b'{"n":2}')) == {"n": 2}
    assert teleop.parse(FakeGBytes(b'{"n":3}')) == {"n": 3}


def test_all_commands():
    for i, cmd in enumerate(teleop.COMMANDS, 1):
        m = msg(seq=i, cmd=cmd)
        if cmd == "SPEED":
            m["arg"] = 50
        out = teleop.validate(m)
        assert out["cmd"] == cmd and out["seq"] == i
        expected = {"t", "seq", "cmd", "ts"} | ({"arg"} if "arg" in m else set())
        assert set(out) == expected, "字段集合不对：%r" % (sorted(out),)


def test_arg_passthrough():
    out = teleop.validate(msg(cmd="BACK", arg=-1000))
    assert out["arg"] == -1000
    out = teleop.validate(msg(cmd="STOP", arg=0))
    assert out["arg"] == 0


def test_ack_rounding():
    a = teleop.ack(7, "FORWARD", 12.5, 3.14159)
    assert a == {"t": "ack", "seq": 7, "cmd": "FORWARD", "ts": 12.5, "ms": 3.142}


def test_overlay_ascii():
    assert teleop.overlay_text(7, "FORWARD", 12, "14:32:05.1") == \
        "CMD #7 FORWARD 12  14:32:05.1"
    assert teleop.overlay_text(3, "STOP", None, "00:00:01.0") == \
        "CMD #3 STOP  00:00:01.0"
    text = teleop.overlay_text(99, "SPEED", 50, "23:59:59.9")
    text.encode("ascii")          # 板子没有中文字体，混进非 ASCII 就是方框


def test_nack_from():
    exc = expect_reason("unknown-command", msg=msg(seq=9, cmd="BOGUS"))
    n = teleop.nack_from(exc)
    assert n == {"t": "nack", "seq": 9, "cmd": "BOGUS",
                 "ts": 1728000000000.0, "reason": "unknown-command"}


def test_parse_errors():
    expect_reason("bad-json", raw="{")
    expect_reason("bad-json", raw=b"\xff\xfe")
    expect_reason("too-large", raw="x" * (teleop.MAX_MESSAGE_BYTES + 1))
    expect_reason("bad-type", raw='"just a string"')
    expect_reason("bad-type", raw=None, msg=None)


def test_bad_seq():
    for bad in (None, 0, -1, True, "1", 1.5):
        m = msg()
        if bad is None:
            m.pop("seq")
        else:
            m["seq"] = bad
        expect_reason("bad-seq", msg=m)


def test_bad_cmd():
    m = msg()
    m.pop("cmd")
    expect_reason("bad-cmd", msg=m)
    expect_reason("bad-cmd", msg=msg(cmd=123))


def test_bad_type():
    expect_reason("bad-type", msg=msg(t="ack"))
    expect_reason("bad-type", raw="[1,2,3]")


def test_bad_ts():
    for bad in (None, -1, True, float("nan"), float("inf")):
        m = msg()
        if bad is None:
            m.pop("ts")
        else:
            m["ts"] = bad
        expect_reason("bad-ts", msg=m)
    # json.loads 默认接受 NaN / Infinity 字面量 —— 必须在 validate 里挡掉，
    # 否则它们会一路写进 nack 的 ts，连 json.dumps 都可能出错。
    expect_reason("bad-ts", raw='{"t":"cmd","seq":1,"cmd":"STOP","ts":NaN}')


def test_bad_arg():
    for bad in (1.5, True, 1001, -1001, "12"):
        expect_reason("bad-arg", msg=msg(cmd="SPEED", arg=bad))


def test_missing_arg():
    expect_reason("missing-arg", msg=msg(cmd="SPEED"))


def test_connect_message():
    ch = FakeChannel({"on-message-string", "on-message-data"})
    assert teleop.connect_message(ch, lambda *a: None) == "on-message-string"
    assert ch.connected == ["on-message-string"]

    ch = FakeChannel({"on-message-data"})
    assert teleop.connect_message(ch, lambda *a: None) == "on-message-data"

    ch = FakeChannel({"on-message"})
    assert teleop.connect_message(ch, lambda *a: None) == "on-message"

    ch = FakeChannel(set())
    try:
        teleop.connect_message(ch, lambda *a: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("一个信号都没有时应当抛 RuntimeError")


def main():
    tests = [(name[5:], fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        check(name, fn)
    print()
    if FAILS:
        print("%d / %d 项失败" % (len(FAILS), len(tests)))
        return 1
    print("全部 %d 项通过" % len(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
