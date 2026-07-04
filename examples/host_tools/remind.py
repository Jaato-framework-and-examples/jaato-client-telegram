"""
Scheduled Reminders Tool (timezone-aware, persistent across restarts)

Actions:
  set_tz  – set your IANA timezone once (e.g. "Europe/Madrid"); absolute-time
            reminders are then interpreted in it
  remind  – schedule a reminder; when it fires it WAKES the assistant
  list    – show all active reminders
  cancel  – cancel a reminder by its ID

All scheduling math is done in UTC (the host clock may be UTC, e.g. on a VPS).
An absolute ``time`` (HH:MM) is interpreted in the user's SAVED timezone — never
the host's — so "07:00" means the user's 07:00. ``delay_minutes`` is relative and
timezone-independent. Reminders persist to JSON and are re-armed at bot startup
(see ``on_startup``), so they survive restarts.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TOOL_SCHEMA = {
    "name": "remind",
    "description": (
        "Create, list, or cancel scheduled reminders. When a reminder fires it "
        "WAKES the assistant (resuming the session if it went idle) to tell the "
        "user and act on it — not a static message. An absolute 'time' (HH:MM) is "
        "interpreted in the user's timezone; set it once with action='set_tz'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["remind", "list", "cancel", "set_tz"],
                "description": "'remind' creates one, 'list' shows pending, 'cancel' removes one, 'set_tz' saves your timezone."
            },
            "text": {
                "type": "string",
                "description": "The reminder message text. Required for 'remind'."
            },
            "delay_minutes": {
                "type": "integer",
                "description": "Minutes from now to fire (timezone-independent). Mutually exclusive with 'time'."
            },
            "time": {
                "type": "string",
                "description": "Absolute time to fire, 'HH:MM' 24h, interpreted in your saved timezone (set_tz first). If already past today, targets tomorrow."
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone name (e.g. Europe/Madrid, America/New_York). Required for 'set_tz'."
            },
            "reminder_id": {
                "type": "string",
                "description": "ID of the reminder to cancel (as shown by 'list'). Required for 'cancel'."
            }
        },
        "required": ["action"]
    }
}

STORE_PATH = Path(__file__).parent / "reminders.json"
_TZ_PATH = Path(__file__).parent / "reminder_timezone.txt"

_reminders: dict[str, asyncio.Task] = {}
_next_id = 0
_ctx = None  # set on first execute call, used for re-scheduling after restart


def _now() -> datetime:
    """Current time as an AWARE UTC datetime — all scheduling math is in UTC, so
    it never depends on the host's local timezone (UTC on the VPS)."""
    return datetime.now(timezone.utc)


def _load_tz() -> str:
    try:
        return _TZ_PATH.read_text().strip() if _TZ_PATH.exists() else ""
    except OSError:
        return ""


def _save_tz(tz: str) -> None:
    _TZ_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TZ_PATH.write_text(tz)


def _make_id() -> str:
    global _next_id
    _next_id += 1
    return f"r{_next_id}"


def _target_from_delay(delay_minutes: int) -> datetime:
    return _now() + timedelta(minutes=delay_minutes)


def _target_from_time(time_str: str, tz_str: str) -> datetime:
    """Interpret HH:MM in the user's timezone, return an AWARE UTC datetime. If
    that clock time already passed locally today, target tomorrow."""
    tz = ZoneInfo(tz_str)
    now_local = datetime.now(tz)
    hour, minute = map(int, time_str.split(":"))
    target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_local <= now_local:
        target_local += timedelta(days=1)
    return target_local.astimezone(timezone.utc)


def _save():
    """Serialize all active (non-done, non-cancelled) reminders to disk."""
    now = _now()
    data = []
    for rid, task in _reminders.items():
        if task.done():
            continue
        data.append({
            "id": rid,
            "text": getattr(task, "_text", ""),
            "target": getattr(task, "_target", now).isoformat(),
            "chat_id": getattr(task, "_chat_id", 0),
        })
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, indent=2))


def _load() -> list[dict]:
    """Load persisted reminders from disk."""
    if not STORE_PATH.exists():
        return []
    try:
        return json.loads(STORE_PATH.read_text())
    except (json.JSONDecodeError, KeyError):
        return []


def _wake_prompt(text: str) -> str:
    return (
        f"\u23f0 A scheduled reminder just fired: \"{text}\". "
        f"Let the user know now, and take any action it implies."
    )


async def _fire(wake, chat_id: int, text: str, rid: str, target: datetime):
    now = _now()
    if target > now:
        await asyncio.sleep((target - now).total_seconds())
    try:
        # WAKE THE MODEL with the fired reminder as an event: wake(chat_id, ...)
        # resumes the session if it went idle and runs a turn, so the assistant
        # can tell the user and take any action the reminder implies. (A plain
        # bot.send_message would post text but never involve the model.) It defers
        # behind any in-flight user turn rather than interrupting it. `wake` is the
        # raw pump wake (ctx.wake_fn) \u2014 it takes chat_id, so a reminder restored at
        # bot startup fires for its OWN chat, not the current one.
        wake(chat_id, _wake_prompt(text))
    except Exception:
        pass
    finally:
        _reminders.pop(rid, None)
        _save()


def _schedule(wake, chat_id: int, rid: str, text: str, target: datetime):
    loop = asyncio.get_running_loop()
    task = loop.create_task(_fire(wake, chat_id, text, rid, target))
    task._rid = rid
    task._text = text
    task._target = target
    task._chat_id = chat_id
    _reminders[rid] = task


async def _restore(wake) -> int:
    """Re-schedule persisted reminders still in the future, each for its OWN
    chat_id (read from the store) \u2014 so a restart re-arms them correctly."""
    global _next_id
    now = _now()
    restored = 0
    for entry in _load():
        rid = entry["id"]
        target = datetime.fromisoformat(entry["target"])
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)  # legacy naive → assume UTC
        if target <= now:
            continue  # already expired, skip
        _schedule(wake, entry.get("chat_id", 0), rid, entry["text"], target)
        # keep _next_id above any restored ID
        try:
            num = int(rid[1:])
            if num >= _next_id:
                _next_id = num + 1
        except ValueError:
            pass
        restored += 1
    if restored:
        _save()
    return restored


async def on_startup(wake) -> int:
    """Bot-startup hook (called by SessionPool.run_host_tool_startup): re-arm
    persisted reminders that a restart dropped. ``wake(chat_id, text)`` is the
    pump wake. Durable reminders survive bot restarts through this."""
    if not _reminders and _load():
        return await _restore(wake)
    return 0


async def execute(args: dict, ctx) -> dict:
    global _next_id, _ctx

    # stash ctx for future restores (e.g. after restart)
    _ctx = ctx

    # Raw pump wake: wake(chat_id, text) resumes the session + runs a turn. None
    # when no pump is wired (feature off). on_startup already restores at boot;
    # this covers a tool loaded AFTER boot (its on_startup never ran).
    wake = ctx.wake_fn
    if wake is not None and not _reminders and _load():
        await _restore(wake)

    action = args["action"]

    # ---- LIST ----
    if action == "list":
        now = _now()
        lines = []
        for rid, task in _reminders.items():
            if task.done():
                continue
            text = getattr(task, "_text", "?")
            target = getattr(task, "_target", None)
            if target:
                remaining = int((target - now).total_seconds())
                mins, secs = divmod(max(remaining, 0), 60)
                lines.append(f"  \u2022 {rid} \u2014 {text} (fires in {mins}m {secs}s)")
            else:
                lines.append(f"  \u2022 {rid} \u2014 {text} (active)")
        if not lines:
            return {"result": "No active reminders."}
        return {"result": "Active reminders:\n" + "\n".join(lines)}

    # ---- CANCEL ----
    if action == "cancel":
        rid = args.get("reminder_id", "")
        task = _reminders.pop(rid, None)
        if task and not task.done():
            task.cancel()
            _save()
            return {"result": f"Reminder {rid} cancelled."}
        return {"error": f"No active reminder with ID {rid}."}

    # ---- SET_TZ ----
    if action == "set_tz":
        tz_str = (args.get("timezone") or "").strip()
        if not tz_str:
            return {"error": "'timezone' is required for set_tz (e.g. Europe/Madrid)."}
        try:
            ZoneInfo(tz_str)
        except Exception:
            return {"error": (
                f"Invalid timezone: {tz_str!r}. Use IANA names like Europe/Madrid "
                f"or America/New_York."
            )}
        _save_tz(tz_str)
        now_local = datetime.now(ZoneInfo(tz_str))
        return {"result": (
            f"Timezone set to {tz_str} — your local time is "
            f"{now_local.strftime('%H:%M, %d %b %Y')}. Absolute-time reminders "
            f"('time') will use it."
        )}

    # ---- REMIND ----
    text = args.get("text", "").strip()
    if not text:
        return {"error": "'text' is required for remind action."}

    delay = args.get("delay_minutes")
    time_str = args.get("time")

    if delay is not None and time_str:
        return {"error": "Provide either delay_minutes or time, not both."}

    if delay is not None:
        target = _target_from_delay(delay)
        label = f"in {delay} min"
    elif time_str:
        tz_str = _load_tz()
        if not tz_str:
            return {"error": (
                "No timezone set — I need it to interpret an absolute time "
                "correctly (the host clock may be UTC). Set it once with "
                "action='set_tz' (e.g. timezone='Europe/Madrid'), or use "
                "delay_minutes instead."
            )}
        target = _target_from_time(time_str, tz_str)
        label = f"at {time_str} ({tz_str})"
    else:
        return {"error": "Provide either delay_minutes or time."}

    if wake is None:
        return {"error": "Reminder delivery is unavailable (no wake capability wired)."}

    rid = _make_id()
    _schedule(wake, ctx.chat_id, rid, text, target)
    _save()

    wait_secs = (target - _now()).total_seconds()
    return {
        "result": (
            f"Reminder set! \U0001f4c5\n"
            f"  ID: {rid}\n"
            f"  {label} (\u2248 {int(wait_secs // 60)}m {int(wait_secs % 60)}s)\n"
            f"  Text: {text}"
        )
    }
