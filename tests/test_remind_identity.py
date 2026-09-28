"""A recurring reminder is ONE reminder however many rows the store holds.

Regression (2026-09-28, the "six 07:00 briefings" incident): a recurring reminder
re-scheduled itself under a freshly minted id on every firing, so nothing could
recognise a reminder it already had. The store accumulated one identical row per
firing — six, verbatim, same chat, same text, same target — and ``_restore`` armed
a timer for each. One reminder, six wakes, six briefings on a paid audio model.

The fix gives a recurring reminder an identity derived from what makes it the
same reminder (chat, clock time, timezone, recurrence, text), so a duplicate
collides with itself instead of adding.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jaato_client_telegram.host_tool_loader import load_tool_file

REMIND = Path("examples/host_tools/remind.py")


def _load(tmp_path):
    _schema, execute = load_tool_file(REMIND)
    g = execute.__globals__
    g["STORE_PATH"] = tmp_path / "reminders.json"
    g["_TZ_PATH"] = tmp_path / "reminder_timezone.txt"
    return g


def _daily_rows(n, target):
    """n verbatim copies of one daily reminder — the shape found on the box."""
    return [
        {
            "id": f"r{215 + i}",
            "text": "weather and ephemerides",
            "target": target,
            "chat_id": 8264086180,
            "recurrence": "daily",
            "time_str": "07:00",
            "tz_str": "Europe/Madrid",
        }
        for i in range(n)
    ]


def test_six_identical_rows_arm_one_timer(tmp_path):
    async def run():
        target = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat()
        (tmp_path / "reminders.json").write_text(json.dumps(_daily_rows(6, target)))
        g = _load(tmp_path)

        calls = []
        n = await g["on_startup"](lambda cid, text: calls.append((cid, text)))

        assert n == 1, f"armed {n} timers for one reminder"
        await asyncio.sleep(0.6)
        assert len(calls) == 1, f"fired {len(calls)} times for one reminder"
    asyncio.run(run())


def test_the_store_does_not_grow_across_firings(tmp_path):
    """After firing, the rescheduled reminder must REPLACE its row, not add one."""
    async def run():
        target = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat()
        store = tmp_path / "reminders.json"
        store.write_text(json.dumps(_daily_rows(6, target)))
        g = _load(tmp_path)

        await g["on_startup"](lambda cid, text: None)
        await asyncio.sleep(0.6)          # fire + reschedule + save

        rows = json.loads(store.read_text())
        assert len(rows) == 1, f"store holds {len(rows)} rows after one firing"
        assert rows[0]["recurrence"] == "daily"   # still armed for tomorrow
    asyncio.run(run())


def test_the_id_survives_a_firing(tmp_path):
    """A firing must REUSE the id, not mint one — minting is what grew the store.

    Compared across the firing inside ONE module, because that is where the old
    code minted: ``_fire`` rescheduled under ``_make_id()``.
    """
    async def run():
        target = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat()
        store = tmp_path / "reminders.json"
        store.write_text(json.dumps(_daily_rows(1, target)))
        g = _load(tmp_path)

        await g["on_startup"](lambda cid, text: None)
        before = json.loads(store.read_text())[0]["id"]

        await asyncio.sleep(0.6)          # fire + reschedule + save
        after = json.loads(store.read_text())[0]["id"]

        assert before == after, f"id changed across a firing: {before} -> {after}"
    asyncio.run(run())


def test_on_unload_cancels_armed_timers(tmp_path):
    """A replaced module must not leave its timers firing beside the new one."""
    async def run():
        target = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat()
        (tmp_path / "reminders.json").write_text(json.dumps(_daily_rows(1, target)))
        g = _load(tmp_path)

        calls = []
        await g["on_startup"](lambda cid, text: calls.append(text))
        g["on_unload"]()
        await asyncio.sleep(0.6)

        assert calls == [], "a cancelled timer still fired"
    asyncio.run(run())
