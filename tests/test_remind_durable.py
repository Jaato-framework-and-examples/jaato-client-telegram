"""remind.py durability: on_startup() re-arms persisted reminders after a bot
restart, each firing for its OWN chat via the raw wake(chat_id, text).
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from jaato_client_telegram.host_tool_loader import load_tool_file

REMIND = Path("examples/host_tools/remind.py")


def _load(tmp_store):
    """Fresh remind.py module with its store redirected to a temp file."""
    _schema, execute = load_tool_file(REMIND)
    g = execute.__globals__
    g["STORE_PATH"] = tmp_store  # never touch the repo's reminders.json
    return g


def test_on_startup_rearms_future_reminder_for_its_own_chat(tmp_path):
    async def run():
        store = tmp_path / "reminders.json"
        target = (datetime.now() + timedelta(seconds=0.3)).isoformat()
        store.write_text(json.dumps([
            {"id": "r1", "text": "water plants", "target": target, "chat_id": 999},
        ]))
        g = _load(store)

        calls: list[tuple] = []
        n = await g["on_startup"](lambda cid, text: calls.append((cid, text)))
        assert n == 1  # one reminder re-armed at startup

        await asyncio.sleep(0.6)  # let the re-armed timer fire
        assert len(calls) == 1
        cid, text = calls[0]
        assert cid == 999                 # fired for ITS chat (from the store), not "current"
        assert "water plants" in text     # the wake prompt carries the reminder
    asyncio.run(run())


def test_on_startup_skips_already_expired(tmp_path):
    async def run():
        store = tmp_path / "reminders.json"
        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        store.write_text(json.dumps([
            {"id": "r1", "text": "old", "target": past, "chat_id": 1},
        ]))
        g = _load(store)
        n = await g["on_startup"](lambda cid, text: None)
        assert n == 0
    asyncio.run(run())


def test_on_startup_empty_store_noop(tmp_path):
    async def run():
        store = tmp_path / "reminders.json"  # does not exist
        g = _load(store)
        n = await g["on_startup"](lambda cid, text: None)
        assert n == 0
    asyncio.run(run())


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
