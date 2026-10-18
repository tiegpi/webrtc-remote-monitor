#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信令服务器 / 板端会话路由回归测试（不需要 GStreamer / 摄像头 / 浏览器）。

覆盖：
  1. 第二个发布端收到 room-busy（不能在 ws.prepare() 之后重复 prepare）
  2. bad-token 仍然在 prepare 之前被拒
  3. 发布端发来的 error 按 viewer 路由，code 字段透传
  4. 第二个观看端被拒后，它的 answer/candidate 不污染当前会话，
     而且它离开时**不能**把当前观看端的会话一起停掉
  5. Session.start()/stop() 会投递到 GLib 线程，且在 GLib 线程内调用不自锁
"""
import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import device_agent                          # noqa: E402
import signal_server                         # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


def make_args(room="demo", token="hunter2"):
    return SimpleNamespace(
        room=room,
        token=token,
        camera_source="mipi",
        ice_mode="none",
        ice_ttl=300,
        ice_policy="all",
        public_ip="127.0.0.1",
        turn_port=3478,
        turn_secret="test-secret",
        signal="ws://127.0.0.1/ws/publish",
        signal_auth=None,
        tls_ca=None,
        ice_url=None,
        stun="",
        turn="",
    )


async def new_client():
    client = TestClient(TestServer(signal_server.build_app(make_args())))
    await client.start_server()
    return client


async def wait_for(predicate, timeout=3.0, what="条件"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待超时：%s" % what)


async def wait_msg(ws, timeout=2.0):
    try:
        return await asyncio.wait_for(ws.receive_json(), timeout)
    except asyncio.TimeoutError:
        return None


async def wait_type(ws, want, timeout=2.0):
    """收消息直到拿到指定 type（跳过 ice-servers 这类前导消息）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        msg = await wait_msg(ws, max(0.01, end - time.monotonic()))
        if msg is None:
            break
        if msg.get("type") == want:
            return msg
    raise AssertionError("没有收到 %r" % want)


# --------------------------------------------------------------------------- #
# 用例 1：第二个发布端
# --------------------------------------------------------------------------- #
async def case_second_publisher_gets_room_busy():
    client = await new_client()
    try:
        a = await client.ws_connect("/ws/publish?room=demo&token=hunter2")
        await wait_for(lambda: client.server.app["room"].publisher is not None,
                       what="第一个发布端上线")
        b = await client.ws_connect("/ws/publish?room=demo&token=hunter2")
        msg = await wait_msg(b)
        assert msg and msg.get("type") == "error", msg
        assert msg.get("code") == "room-busy", msg
        await b.close()
        await a.close()
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# 用例 2：bad-token
# --------------------------------------------------------------------------- #
async def case_bad_token_still_rejected():
    client = await new_client()
    try:
        ws = await client.ws_connect("/ws/publish?room=demo&token=wrong")
        msg = await wait_msg(ws)
        assert msg and msg.get("code") == "bad-token", msg
        await ws.close()
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# 用例 3：发布端 error 按 viewer 路由，code 透传
# --------------------------------------------------------------------------- #
async def case_error_is_routed_by_viewer():
    client = await new_client()
    try:
        pub = await client.ws_connect("/ws/publish?room=demo&token=hunter2")
        await wait_for(lambda: client.server.app["room"].publisher is not None,
                       what="发布端上线")

        viewer = await client.ws_connect("/ws/view?room=demo")
        joined = await wait_type(pub, "viewer-joined")
        vid = joined["viewer"]

        await pub.send_json({"type": "error", "viewer": "nope",
                             "code": "device-busy", "message": "不该送到"})
        assert await wait_msg(viewer, 0.3) is None, "viewer 不存在时不应投递"

        await pub.send_json({"type": "error", "viewer": vid,
                             "code": "device-busy", "message": "设备忙"})
        msg = await wait_msg(viewer)
        assert msg and msg.get("code") == "device-busy", msg
        assert msg.get("message") == "设备忙", msg

        await viewer.close()
        await pub.close()
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# 用例 4：第二个观看端不能拆掉第一个的会话
# --------------------------------------------------------------------------- #
class FakeSession:
    instances = []

    def __init__(self, send, loop, args, ice, source=None):
        self.send = send
        self.stopped = False
        self.stop_calls = 0
        self.signals = []
        FakeSession.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True
        self.stop_calls += 1

    def on_signal(self, data):
        self.signals.append(data)

    def counters(self):
        return {}


async def case_second_viewer_cannot_teardown_first():
    client = await new_client()
    original = device_agent.DeviceSession
    device_agent.DeviceSession = FakeSession
    FakeSession.instances.clear()
    state = device_agent.AgentState(make_args())
    pub_task = None
    try:
        args = make_args()
        ws_url = str(client.server.make_url("/ws/publish"))
        ws_url = ws_url.replace("http://", "ws://")
        ws_url += "?room=demo&token=hunter2"
        ice_url = str(client.server.make_url("/ice.json"))
        pub_task = asyncio.create_task(
            device_agent.publish_once(args, state, ws_url, ice_url)
        )
        await wait_for(lambda: client.server.app["room"].publisher is not None,
                       what="发布端上线")

        a = await client.ws_connect("/ws/view?room=demo")
        await wait_for(lambda: state.session is not None, what="观看端 A 建会话")
        first = state.session
        vid_a = state.viewer_id
        assert vid_a, "会话必须记住 viewer id"

        b = await client.ws_connect("/ws/view?room=demo")
        msg = await wait_msg(b)
        assert msg and msg.get("code") == "device-busy", msg

        # B 的 answer/candidate 不能进入当前会话
        await b.send_json({"type": "answer", "sdp": "from-b"})
        await b.send_json({"type": "candidate", "candidate": "from-b"})
        await asyncio.sleep(0.2)
        assert first.signals == [], "非当前观看端的信令被误用了: %r" % first.signals

        # A 的 answer 正常送达
        await a.send_json({"type": "answer", "sdp": "from-a"})
        await wait_for(lambda: len(first.signals) == 1, what="A 的 answer 送到")
        assert first.signals[0].get("sdp") == "from-a", first.signals

        # **核心回归**：B 断开不能停掉 A 的会话
        await b.close()
        await wait_for(lambda: len(client.server.app["room"].viewers) == 1,
                       what="B 从房间摘除")
        await asyncio.sleep(0.2)
        assert state.session is first, "B 离开后 A 的会话被拆了"
        assert first.stop_calls == 0, "B 离开触发了 stop()"
        assert state.viewer_id == vid_a

        # A 断开才停
        await a.close()
        await wait_for(lambda: state.session is None, what="A 离开后拆会话")
        assert first.stop_calls == 1, "A 离开没有停会话"
        assert first.stopped
    finally:
        device_agent.DeviceSession = original
        if pub_task is not None:
            pub_task.cancel()
            try:
                await pub_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await client.close()


# --------------------------------------------------------------------------- #
# 用例 5：start/stop 投递到 GLib 线程
# --------------------------------------------------------------------------- #
def case_lifecycle_is_marshaled_to_glib():
    from gi.repository import GLib

    loop = GLib.MainLoop()
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    try:
        box = {}

        def record():
            box["thread"] = threading.get_ident()
            return "ok"

        box["caller"] = threading.get_ident()
        from session import _call_on_glib
        assert _call_on_glib(record, "测试") == "ok"
        assert box["thread"] != box["caller"], "没有投递到 GLib 线程"

        # 在 GLib 线程内再调一次，不能自己等自己
        out = {}

        def on_glib():
            out["value"] = _call_on_glib(lambda: "reentrant", "重入")
            return False

        GLib.idle_add(on_glib)
        end = time.monotonic() + 3
        while "value" not in out and time.monotonic() < end:
            time.sleep(0.02)
        assert out.get("value") == "reentrant", "GLib 线程内调用自锁"
    finally:
        loop.quit()
        thread.join(timeout=2)


# --------------------------------------------------------------------------- #
async def main():
    cases = [
        ("第二个发布端收到 room-busy", case_second_publisher_gets_room_busy),
        ("bad-token 仍然被拒", case_bad_token_still_rejected),
        ("error 按 viewer 路由且透传 code", case_error_is_routed_by_viewer),
        ("第二个观看端不能拆掉第一个会话", case_second_viewer_cannot_teardown_first),
    ]
    for name, fn in cases:
        await fn()
        print("PASS  %s" % name)
    case_lifecycle_is_marshaled_to_glib()
    print("PASS  start/stop 投递到 GLib 线程")
    print("\n%d/%d 通过" % (len(cases) + 1, len(cases) + 1))


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main())
