"""Lifecycle tests for SessionPool over the facade WSRecoveryClient.

Verifies the connect → set_workspace → register_client_tools → create/attach
order against a fake client, plus the re-attach-vs-create branch. No real WS I/O.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jaato_sdk.events import EventType

from jaato_client_telegram.session_pool import SessionPool, SessionMetadata


class _FakeClient:
    """Records the lifecycle calls; mimics WSRecoveryClient's surface."""

    def __init__(self, known_sessions=None, attach_answers=None):
        self.calls = []
        self._known = list(known_sessions or [])
        self._list_cb = None
        self._handlers = {}
        # What the daemon emits for an attach, as (event_type, event) pairs;
        # default: SessionInfo if the id is known, "Session not found" if not
        # (server SessionManager.attach_session). [] = no answer at all.
        self._attach_answers = attach_answers
        self.is_connected = False
        self.is_reconnecting = False

    async def connect(self, timeout=5.0):
        self.calls.append(("connect",))
        self.is_connected = True
        return True

    def subscribe(self, event_type, handler):
        # not recorded in calls, so lifecycle call-order assertions stay clean
        self._handlers.setdefault(event_type, []).append(handler)
        return lambda: self._handlers[event_type].remove(handler)

    async def execute_command(self, command, args=None):
        self.calls.append(("execute_command", command, list(args or [])))

    async def register_client_tools(self, tools):
        self.calls.append(("register_client_tools", [t["name"] for t in tools]))

    def subscribe_once(self, event_type, handler):
        if event_type == EventType.SESSION_LIST:
            self._list_cb = handler
        return lambda: None

    async def list_sessions(self):
        self.calls.append(("list_sessions",))
        if self._list_cb:
            self._list_cb(SimpleNamespace(sessions=[{"id": s} for s in self._known]))

    async def attach_session(self, session_id):
        self.calls.append(("attach_session", session_id))
        answers = self._attach_answers
        if answers is None:
            answers = ([(EventType.SESSION_INFO, SimpleNamespace(session_id=session_id))]
                       if session_id in self._known else
                       [(EventType.ERROR, SimpleNamespace(
                           error=f"Session not found: {session_id}", error_type="SessionError"))])
        for event_type, event in answers:
            for handler in list(self._handlers.get(event_type, [])):
                handler(event)
        return True

    async def create_session(self, profile=None, agent=None, cascade_driver_id=None):
        self.calls.append(("create_session", profile, agent))
        return "fresh-sess"

    async def disconnect(self):
        self.calls.append(("disconnect",))


def _make_pool(client, *, store=None, workspace="/ws", profile="p", agent="a"):
    pool = SessionPool.__new__(SessionPool)  # bypass __init__ (needs real config)
    pool._sessions = {}
    pool._lock = asyncio.Lock()
    pool._max_concurrent = 50
    pool._session_store = store
    pool._bot_cid = None      # no cascade id in the lifecycle test
    pool._last_reattach = {}
    pool._bot = None          # skip host-tool assembly for the lifecycle test
    pool._file_config = None
    pool._renderer = None      # no wake watcher in the lifecycle test (feature off)
    pool._watchers = {}
    pool._ws_config = SimpleNamespace(
        url="wss://x", secret_token="", tls=None, workspace=workspace,
        profile=profile, agent=agent, host_tools_dir="", keycloak_client_id="",
    )
    pool._make_client = lambda chat_id=None: client
    return pool


def test_fresh_create_order():
    client = _FakeClient()
    pool = _make_pool(client)
    sid = asyncio.run(pool.get_or_create_session(123))

    assert sid == "fresh-sess"
    kinds = [c[0] for c in client.calls]
    # connect, then create_session (no store → no list/attach). set_workspace is
    # NOT called by us — the client's _handshake sends it on connect.
    assert kinds == ["connect", "create_session"]
    assert client.calls[1] == ("create_session", "p", "a")
    assert pool.took_reattach(123) is False


class _Store:
    def __init__(self, mapping): self._m = dict(mapping)
    def get(self, chat_id): return self._m.get(chat_id)
    def set(self, chat_id, sid): self._m[chat_id] = sid
    def remove(self, chat_id): self._m.pop(chat_id, None)


def test_reattach_when_session_still_known():
    client = _FakeClient(known_sessions=["old-sess"])
    pool = _make_pool(client, store=_Store({7: "old-sess"}))
    sid = asyncio.run(pool.get_or_create_session(7))

    assert sid == "old-sess"
    kinds = [c[0] for c in client.calls]
    assert "attach_session" in kinds and "create_session" not in kinds
    assert ("attach_session", "old-sess") in client.calls
    assert pool.took_reattach(7) is True


def test_create_when_persisted_session_gone():
    client = _FakeClient(known_sessions=[])  # daemon: "Session not found: old-sess"
    pool = _make_pool(client, store=_Store({7: "old-sess"}))
    sid = asyncio.run(pool.get_or_create_session(7))

    assert sid == "fresh-sess"
    kinds = [c[0] for c in client.calls]
    assert kinds.index("attach_session") < kinds.index("create_session")
    assert pool.took_reattach(7) is False


def test_reattach_never_lists_every_session():
    """session.list spans every workspace on the daemon and outgrew the 1 MiB
    frame limit (2026-10-07: 2,092 rows, 1.2 MB) -- each chat then failed."""
    for known in (["old-sess"], []):
        client = _FakeClient(known_sessions=known)
        asyncio.run(_make_pool(client, store=_Store({7: "old-sess"})).get_or_create_session(7))
        assert "list_sessions" not in [c[0] for c in client.calls]


def test_an_error_other_than_not_found_is_raised_not_read_as_gone():
    """'being unloaded; please retry' must not start a fresh conversation."""
    client = _FakeClient(attach_answers=[(EventType.ERROR, SimpleNamespace(
        error="Session old-sess is being unloaded; please retry", error_type="SessionError"))])
    pool = _make_pool(client, store=_Store({7: "old-sess"}))
    try:
        asyncio.run(pool.get_or_create_session(7))
        raise AssertionError("expected the attach error to be raised")
    except RuntimeError as e:
        assert "being unloaded" in str(e)
    assert "create_session" not in [c[0] for c in client.calls]


def test_errors_about_other_sessions_are_not_the_answer():
    client = _FakeClient(attach_answers=[
        (EventType.ERROR, SimpleNamespace(error="Session not found: other", error_type="SessionError")),
        (EventType.SESSION_INFO, SimpleNamespace(session_id="other")),
        (EventType.SESSION_INFO, SimpleNamespace(session_id="old-sess")),
    ])
    pool = _make_pool(client, store=_Store({7: "old-sess"}))
    assert asyncio.run(pool.get_or_create_session(7)) == "old-sess"
    assert "create_session" not in [c[0] for c in client.calls]


def test_another_sessions_info_is_not_an_attach():
    client = _FakeClient(attach_answers=[
        (EventType.SESSION_INFO, SimpleNamespace(session_id="other")),
        (EventType.ERROR, SimpleNamespace(error="Session not found: old-sess", error_type="SessionError")),
    ])
    pool = _make_pool(client, store=_Store({7: "old-sess"}))
    assert asyncio.run(pool.get_or_create_session(7)) == "fresh-sess"


def test_no_answer_raises_instead_of_creating(monkeypatch):
    import jaato_client_telegram.session_pool as sp
    monkeypatch.setattr(sp, "_ATTACH_TIMEOUT_S", 0.05)
    client = _FakeClient(attach_answers=[])
    pool = _make_pool(client, store=_Store({7: "old-sess"}))
    try:
        asyncio.run(pool.get_or_create_session(7))
        raise AssertionError("expected a timeout")
    except RuntimeError:
        pass
    assert "create_session" not in [c[0] for c in client.calls]


def test_the_attach_subscriptions_are_released():
    client = _FakeClient(known_sessions=["old-sess"])
    asyncio.run(_make_pool(client, store=_Store({7: "old-sess"})).get_or_create_session(7))
    assert not client._handlers.get(EventType.SESSION_INFO)
    assert not client._handlers.get(EventType.ERROR)


def test_reuse_live_cached_client():
    client = _FakeClient()
    pool = _make_pool(client)
    asyncio.run(pool.get_or_create_session(123))
    n = len(client.calls)
    # second call reuses the cached, still-connected client — no new lifecycle
    sid2 = asyncio.run(pool.get_or_create_session(123))
    assert sid2 == "fresh-sess"
    assert len(client.calls) == n  # nothing new happened


def _pool_with_workspace(ws):
    pool = SessionPool.__new__(SessionPool)
    pool._ws_config = SimpleNamespace(workspace=str(ws) if ws else "")
    return pool


def test_stage_upload_writes_and_returns_relpath(tmp_path):
    pool = _pool_with_workspace(tmp_path)
    rel = pool.stage_upload("deploy.sh", b"#!/bin/sh\necho hi\n")
    assert rel == "uploads/deploy.sh"
    written = tmp_path / "uploads" / "deploy.sh"
    assert written.read_bytes() == b"#!/bin/sh\necho hi\n"


def test_stage_upload_sanitizes_path_traversal(tmp_path):
    pool = _pool_with_workspace(tmp_path)
    rel = pool.stage_upload("../../etc/passwd", b"x")
    assert rel == "uploads/passwd"                       # basename only — no escape
    assert (tmp_path / "uploads" / "passwd").exists()
    assert not (tmp_path.parent / "etc" / "passwd").exists()


def test_stage_upload_rejects_dotdot_name(tmp_path):
    pool = _pool_with_workspace(tmp_path)
    rel = pool.stage_upload("..", b"x")
    assert rel == "uploads/file"


def test_stage_upload_binary_ok(tmp_path):
    pool = _pool_with_workspace(tmp_path)
    blob = bytes(range(256))
    rel = pool.stage_upload("data.bin", blob)
    assert rel == "uploads/data.bin"
    assert (tmp_path / "uploads" / "data.bin").read_bytes() == blob


def test_stage_upload_none_without_workspace():
    pool = _pool_with_workspace("")
    assert pool.stage_upload("x.txt", b"x") is None


# ── memory curation: the LLM curator on idle-detach ──────────────────────────

def _curator_pool(ws):
    pool = _pool_with_workspace(ws)
    pool._curator_task = None
    pool._internal_conn = lambda: {"url": "ws://x"}   # avoid touching _ws_config
    return pool


def test_curate_memories_bg_noop_without_workspace():
    pool = _curator_pool("")
    asyncio.run(pool.curate_memories_bg())
    assert pool._curator_task is None


def test_curate_memories_bg_skips_empty_raw_queue(monkeypatch):
    import jaato_client_telegram.session_pool as sp
    monkeypatch.setattr(sp, "raw_memory_count", lambda ws: 0)
    pool = _curator_pool("/ws")
    asyncio.run(pool.curate_memories_bg())
    assert pool._curator_task is None   # empty queue → no LLM woken


def test_curate_memories_bg_runs_when_raw_exists(monkeypatch):
    import jaato_client_telegram.session_pool as sp
    monkeypatch.setattr(sp, "raw_memory_count", lambda ws: 3)
    ran = []

    async def fake_run_curator(conn):
        ran.append(conn)

    monkeypatch.setattr(sp, "run_curator", fake_run_curator)
    pool = _curator_pool("/ws")

    async def go():
        await pool.curate_memories_bg()
        assert pool._curator_task is not None
        await pool._curator_task   # let the background drain finish

    asyncio.run(go())
    assert ran == [{"url": "ws://x"}]


def test_curate_memories_bg_skips_when_already_running(monkeypatch):
    import jaato_client_telegram.session_pool as sp
    checked = []
    monkeypatch.setattr(sp, "raw_memory_count", lambda ws: checked.append(1) or 5)
    pool = _curator_pool("/ws")

    async def go():
        pool._curator_task = asyncio.create_task(asyncio.sleep(1))
        await pool.curate_memories_bg()   # a drain is in flight → skip
        pool._curator_task.cancel()

    asyncio.run(go())
    assert checked == []   # returned before even reading the raw count


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"✓ {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} session-pool facade tests passed")
