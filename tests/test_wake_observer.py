"""WakeObserver: routes SessionWokenEvent → chat re-attach, and registers as a
cascade observer for the bot cid."""

import asyncio
from types import SimpleNamespace

import jaato_client_telegram.wake_observer as wo
from jaato_client_telegram.wake_observer import WakeObserver


class _FakePool:
    def __init__(self, mapping):
        self._m = mapping

    def chat_for_session(self, sid):
        return self._m.get(sid)


class _FakePump:
    def __init__(self):
        self.rendered = []

    def wake_render(self, chat_id):
        self.rendered.append(chat_id)


def test_on_woken_routes_to_chat_render():
    pool = _FakePool({"s5": 5})
    pump = _FakePump()
    obs = WakeObserver(lambda: None, "bot-x", pool, pump)

    obs._on_woken(SimpleNamespace(session_id="s5", wake_ref="github-pr:o/r#1",
                                  source="github-pr"))
    assert pump.rendered == [5]

    obs._on_woken(SimpleNamespace(session_id="nope", wake_ref="", source=""))  # unknown
    obs._on_woken(SimpleNamespace(session_id="", wake_ref="", source=""))      # empty
    assert pump.rendered == [5]  # neither fired


class _FakeClient:
    def __init__(self):
        self.subscribed = []
        self.commands = []
        self.is_connected = True
        self.is_reconnecting = False

    async def connect(self):
        return True

    def subscribe(self, event_type, cb):
        self.subscribed.append(event_type)

    async def execute_command(self, cmd, args):
        self.commands.append((cmd, args))
        self.is_connected = False  # end the observe loop after one register

    async def disconnect(self):
        pass


def test_connect_registers_as_cascade_observer(monkeypatch):
    monkeypatch.setattr(wo, "_REREGISTER_INTERVAL", 0.01)

    async def run():
        client = _FakeClient()
        obs = WakeObserver(lambda: client, "bot-x", _FakePool({}), _FakePump())
        await obs._connect_and_observe()
        from jaato_sdk.events import EventType
        assert EventType.SESSION_WOKEN in client.subscribed
        # filter by the event CLASS NAME, not the EventType value (else the cascade
        # tier drops it before our subscribe() handler)
        assert client.commands == [
            ("cascade.register", ["bot-x", "observer", "SessionWokenEvent"]),
        ]
    asyncio.run(run())


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
