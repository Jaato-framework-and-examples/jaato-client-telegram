"""WakeObserver: on a cold SessionWokenEvent it RE-ATTACHES the mapped chat (which
establishes the per-session wake watcher + triggers the daemon's deferred-turn drive
— the watcher renders it). It registers as a cascade observer for the bot cid."""

import asyncio
from types import SimpleNamespace

import jaato_client_telegram.wake_observer as wo
from jaato_client_telegram.wake_observer import WakeObserver


class _FakePool:
    def __init__(self, mapping):
        self._m = mapping
        self.reattached: list[int] = []

    def chat_for_session(self, sid):
        return self._m.get(sid)

    async def get_or_create_session(self, chat_id):
        self.reattached.append(chat_id)
        return "sess"


def test_on_woken_reattaches_mapped_chat():
    async def run():
        pool = _FakePool({"s5": 5})
        obs = WakeObserver(lambda: None, "bot-x", pool)

        obs._on_woken(SimpleNamespace(session_id="s5", wake_ref="github-pr:o/r#1",
                                      source="github-pr"))
        obs._on_woken(SimpleNamespace(session_id="nope", wake_ref="", source=""))  # unknown
        obs._on_woken(SimpleNamespace(session_id="", wake_ref="", source=""))      # empty
        # _on_woken schedules the re-attach via create_task — let those tasks run.
        await asyncio.sleep(0.02)
        assert pool.reattached == [5]  # only the known, non-empty session re-attached
    asyncio.run(run())


class _FakeClient:
    def __init__(self):
        self.subscribed = []
        self.registrations = []
        self.is_connected = True
        self.is_reconnecting = False

    async def connect(self):
        return True

    def subscribe(self, event_type, cb):
        self.subscribed.append(event_type)

    async def cascade_register(self, cid, role, event_types):
        # record the CLASS NAMES the observer passed (the typed method derives them)
        self.registrations.append((cid, role, [e.__name__ for e in event_types]))
        self.is_connected = False  # end the observe loop after one register

    async def disconnect(self):
        pass


def test_connect_registers_as_cascade_observer(monkeypatch):
    monkeypatch.setattr(wo, "_REREGISTER_INTERVAL", 0.01)

    async def run():
        client = _FakeClient()
        obs = WakeObserver(lambda: client, "bot-x", _FakePool({}))
        await obs._connect_and_observe()
        from jaato_sdk.events import EventType
        assert EventType.SESSION_WOKEN in client.subscribed
        # cascade_register receives the event CLASS; it derives the class name (the
        # cascade tier filters by class name, not the EventType value "session.woken")
        assert client.registrations == [
            ("bot-x", "observer", ["SessionWokenEvent"]),
        ]
    asyncio.run(run())


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
