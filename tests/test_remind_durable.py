"""remind.py durability: on_startup() re-arms persisted reminders after a bot
restart, each firing for its OWN chat via the raw wake(chat_id, text).
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from jaato_client_telegram.host_tool_loader import load_tool_file

REMIND = Path("examples/host_tools/remind.py")


def _load(tmp_path):
    """Fresh remind.py module with its store + tz file redirected to temp."""
    _schema, execute = load_tool_file(REMIND)
    g = execute.__globals__
    g["STORE_PATH"] = tmp_path / "reminders.json"   # never touch the repo's store
    g["_TZ_PATH"] = tmp_path / "reminder_timezone.txt"
    return g


def test_on_startup_rearms_future_reminder_for_its_own_chat(tmp_path):
    async def run():
        store = tmp_path / "reminders.json"
        target = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat()
        store.write_text(json.dumps([
            {"id": "r1", "text": "water plants", "target": target, "chat_id": 999},
        ]))
        g = _load(tmp_path)

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
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        store.write_text(json.dumps([
            {"id": "r1", "text": "old", "target": past, "chat_id": 1},
        ]))
        g = _load(tmp_path)
        n = await g["on_startup"](lambda cid, text: None)
        assert n == 0
    asyncio.run(run())


def test_on_startup_empty_store_noop(tmp_path):
    async def run():
        g = _load(tmp_path)  # store does not exist
        n = await g["on_startup"](lambda cid, text: None)
        assert n == 0
    asyncio.run(run())


# --- timezone safety ---------------------------------------------------------

def test_absolute_time_interpreted_in_user_tz_not_host(tmp_path):
    """The core tz fix: 'HH:MM' is the USER's local time, not the host's (UTC on
    the VPS). 07:00 Europe/Madrid must map to a UTC instant whose Madrid-local
    clock reads 07:00 — regardless of the host timezone the test runs under."""
    g = _load(tmp_path)
    target_utc = g["_target_from_time"]("07:00", "Europe/Madrid")
    assert target_utc.tzinfo is not None                      # aware
    assert target_utc.utcoffset().total_seconds() == 0        # stored in UTC
    local = target_utc.astimezone(ZoneInfo("Europe/Madrid"))
    assert (local.hour, local.minute) == (7, 0)               # 07:00 in Madrid


def test_set_tz_persists_and_is_used(tmp_path):
    async def run():
        g = _load(tmp_path)
        ctx = SimpleNamespace(wake_fn=lambda c, t: None, chat_id=1)
        r = await g["execute"]({"action": "set_tz", "timezone": "America/New_York"}, ctx)
        assert "America/New_York" in r["result"]
        assert g["_load_tz"]() == "America/New_York"
        # bad tz rejected
        bad = await g["execute"]({"action": "set_tz", "timezone": "Not/AZone"}, ctx)
        assert "error" in bad
    asyncio.run(run())


def test_absolute_time_without_tz_errors(tmp_path):
    async def run():
        g = _load(tmp_path)
        ctx = SimpleNamespace(wake_fn=lambda c, t: None, chat_id=1)
        r = await g["execute"]({"action": "remind", "text": "x", "time": "07:00"}, ctx)
        assert "error" in r and "timezone" in r["error"].lower()
    asyncio.run(run())


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
