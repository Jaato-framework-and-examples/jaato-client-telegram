"""Client-side wake-binding wiring: ToolContext.bind_wake/unbind_wake → the pool
command → WSRecoveryClient.execute_command + WakeBindResultEvent round-trip.

The server half (session.bind_wake / WakeBindResultEvent) lives in jaato-server;
here we verify only that the bot sends the right command/args and surfaces the
outcome, and that the feature is a clean no-op when unwired.
"""

import asyncio
from types import SimpleNamespace

from jaato_client_telegram.host_tool_loader import ToolContext
from jaato_client_telegram.session_pool import SessionPool


# ---- ToolContext surface ---------------------------------------------------

def test_ctx_bind_wake_calls_bind_fn_and_noops_without():
    async def run():
        calls = []

        async def bind_fn(cid, wake_ref, keys):
            calls.append((cid, wake_ref, keys))
            return {"outcome": "ok", "expires_at": 123.0}

        ctx = ToolContext(bot=None, chat_id=7, bind_fn=bind_fn)
        r = await ctx.bind_wake("github-pr:o/r#1", ["PEMKEY"])
        assert r["outcome"] == "ok"
        assert calls == [(7, "github-pr:o/r#1", ["PEMKEY"])]

        # no bind_fn wired -> disabled, never raises
        r2 = await ToolContext(bot=None, chat_id=7).bind_wake("x", ["k"])
        assert r2["outcome"] == "disabled"
    asyncio.run(run())


def test_ctx_unbind_wake_calls_unbind_fn_and_noops_without():
    async def run():
        calls = []

        async def unbind_fn(cid, wake_ref):
            calls.append((cid, wake_ref))
            return {"outcome": "ok"}

        ctx = ToolContext(bot=None, chat_id=5, unbind_fn=unbind_fn)
        assert (await ctx.unbind_wake("r"))["outcome"] == "ok"
        assert calls == [(5, "r")]
        assert (await ToolContext(bot=None, chat_id=5).unbind_wake("r"))["outcome"] == "disabled"
    asyncio.run(run())


# ---- pool command (execute_command + WakeBindResult round-trip) ------------

class _FakeClient:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.is_reconnecting = False
        self.sent: list = []
        self._cb = None

    def subscribe_once(self, event_type, cb):
        self._cb = cb
        return lambda: None

    async def execute_command(self, command, args):
        self.sent.append((command, args))
        # simulate the daemon replying with a WakeBindResultEvent
        self._cb(SimpleNamespace(
            wake_ref=args[0], outcome="ok", expires_at=99.0, detail="bound"))


def test_wake_binding_command_sends_and_returns_result():
    async def run():
        fc = _FakeClient()
        me = SimpleNamespace(_sessions={7: SimpleNamespace(client=fc)})
        r = await SessionPool._wake_binding_command(
            me, 7, "session.bind_wake", ["github-pr:o/r#1", "PEM"], "github-pr:o/r#1")
        assert r == {"outcome": "ok", "expires_at": 99.0, "detail": "bound"}
        assert fc.sent == [("session.bind_wake", ["github-pr:o/r#1", "PEM"])]
    asyncio.run(run())


def test_wake_binding_command_no_live_session():
    async def run():
        me = SimpleNamespace(_sessions={})
        r = await SessionPool._wake_binding_command(me, 7, "session.bind_wake", ["r"], "r")
        assert r["outcome"] == "no_session"
    asyncio.run(run())


def test_bind_unbind_wrappers_format_args():
    async def run():
        seen = []

        async def fake_core(chat_id, command, args, wake_ref):
            seen.append((command, args, wake_ref))
            return {"outcome": "ok"}

        me = SimpleNamespace(_wake_binding_command=fake_core)
        await SessionPool.bind_wake_command(me, 7, "ref", ["k1", "k2"])
        await SessionPool.unbind_wake_command(me, 7, "ref")
        assert seen == [
            ("session.bind_wake", ["ref", "k1", "k2"], "ref"),
            ("session.unbind_wake", ["ref"], "ref"),
        ]
    asyncio.run(run())


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
