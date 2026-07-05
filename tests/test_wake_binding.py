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


# ---- pool command (typed client.bind_wake/unbind_wake, jaato-sdk #525) ------

class _FakeClient:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.is_reconnecting = False
        self.calls: list = []

    async def bind_wake(self, wake_ref, trust_keys):
        self.calls.append(("bind", wake_ref, trust_keys))
        # the typed method returns the daemon's WakeBindResultEvent (incl. the daemon-
        # reported wake endpoint, which it knows from its own wake.json)
        return SimpleNamespace(wake_ref=wake_ref, outcome="ok", expires_at=99.0,
                               detail="bound", endpoint="https://daemon.example/wake")

    async def unbind_wake(self, wake_ref):
        self.calls.append(("unbind", wake_ref))
        return SimpleNamespace(wake_ref=wake_ref, outcome="ok", expires_at=0.0,
                               detail="unbound", endpoint="")


def test_wake_binding_calls_typed_method_and_returns_result():
    async def run():
        fc = _FakeClient()
        me = SimpleNamespace(_sessions={7: SimpleNamespace(client=fc)})
        r = await SessionPool._wake_binding(me, 7, "github-pr:o/r#1", ["PEM"])
        assert r == {"outcome": "ok", "expires_at": 99.0, "detail": "bound",
                     "endpoint": "https://daemon.example/wake"}
        assert fc.calls == [("bind", "github-pr:o/r#1", ["PEM"])]
    asyncio.run(run())


def test_wake_binding_no_live_session():
    async def run():
        me = SimpleNamespace(_sessions={})
        r = await SessionPool._wake_binding(me, 7, "r", ["k"])
        assert r["outcome"] == "no_session"
    asyncio.run(run())


def test_bind_unbind_wrappers_dispatch_to_binding():
    async def run():
        seen = []

        async def fake_binding(chat_id, wake_ref, trust_keys):
            seen.append((chat_id, wake_ref, trust_keys))
            return {"outcome": "ok"}

        me = SimpleNamespace(_wake_binding=fake_binding)
        await SessionPool.bind_wake_command(me, 7, "ref", ["k1", "k2"])
        await SessionPool.unbind_wake_command(me, 7, "ref")
        assert seen == [
            (7, "ref", ["k1", "k2"]),   # bind → trust_keys passed through
            (7, "ref", None),           # unbind → None (dispatches to client.unbind_wake)
        ]
    asyncio.run(run())


# ---- per-bot cascade id + reverse lookup (for the WakeObserver) ------------

def test_bot_cid_stable_across_restart(tmp_path):
    """The per-bot cascade id is generated once + persisted, so a restart reuses it
    (else a new cid wouldn't observe pre-restart sessions)."""
    p = str(tmp_path / "sessions.json")
    c1 = SessionPool._load_or_create_bot_cid(p)
    c2 = SessionPool._load_or_create_bot_cid(p)  # "restart"
    assert c1 == c2 and c1.startswith("bot-")
    assert (tmp_path / "bot_wake_cid").read_text(encoding="utf-8").strip() == c1


def test_pool_chat_for_session_reverse(tmp_path):
    """session_id -> chat_id, in-memory first then the persistent store."""
    from jaato_client_telegram.chat_session_store import ChatSessionStore

    pool = SessionPool.__new__(SessionPool)
    pool._sessions = {5: SimpleNamespace(session_id="s5")}
    pool._session_store = None
    assert pool.chat_for_session("s5") == 5
    assert pool.chat_for_session("absent") is None

    store = ChatSessionStore(str(tmp_path / "s.json"))
    store.set(9, "s9")
    pool._sessions = {}
    pool._session_store = store
    assert pool.chat_for_session("s9") == 9


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
