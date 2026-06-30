"""
Scheduled Reminders Tool (persistent across restarts)

Actions:
  remind  – schedule a reminder (sends via Telegram when due)
  list    – show all active reminders
  cancel  – cancel a reminder by its ID

Reminders are persisted to a JSON file so they survive bot restarts.
On load, any future reminders from the file are re-scheduled automatically.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

TOOL_SCHEMA = {
    "name": "remind",
    "description": "Create, list, or cancel scheduled reminders. Reminders are sent as Telegram messages when they fire.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["remind", "list", "cancel"],
                "description": "What to do: 'remind' creates one, 'list' shows pending, 'cancel' removes one."
            },
            "text": {
                "type": "string",
                "description": "The reminder message text. Required for 'remind'."
            },
            "delay_minutes": {
                "type": "integer",
                "description": "Minutes from now to fire the reminder. Mutually exclusive with 'time'."
            },
            "time": {
                "type": "string",
                "description": "Time to fire, in 'HH:MM' 24h format. If that time has already passed today, it targets tomorrow."
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

_reminders: dict[str, asyncio.Task] = {}
_next_id = 0
_ctx = None  # set on first execute call, used for re-scheduling after restart


def _now() -> datetime:
    return datetime.now()


def _make_id() -> str:
    global _next_id
    _next_id += 1
    return f"r{_next_id}"


def _target_from_delay(delay_minutes: int) -> datetime:
    return _now() + timedelta(minutes=delay_minutes)


def _target_from_time(time_str: str) -> datetime:
    now = _now()
    hour, minute = map(int, time_str.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


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


async def _fire(bot, chat_id: int, text: str, rid: str, target: datetime):
    now = _now()
    if target > now:
        await asyncio.sleep((target - now).total_seconds())
    try:
        await bot.send_message(chat_id=chat_id, text=f"\u23f0 **Reminder:** {text}")
    except Exception:
        pass
    finally:
        _reminders.pop(rid, None)
        _save()


def _schedule(bot, chat_id: int, rid: str, text: str, target: datetime):
    loop = asyncio.get_running_loop()
    task = loop.create_task(_fire(bot, chat_id, text, rid, target))
    task._rid = rid
    task._text = text
    task._target = target
    task._chat_id = chat_id
    _reminders[rid] = task


async def _restore(bot, chat_id: int):
    """Re-schedule persisted reminders that are still in the future."""
    global _next_id
    now = _now()
    restored = 0
    for entry in _load():
        rid = entry["id"]
        target = datetime.fromisoformat(entry["target"])
        if target <= now:
            continue  # already expired, skip
        _schedule(bot, chat_id, rid, entry["text"], target)
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


async def execute(args: dict, ctx) -> dict:
    global _next_id, _ctx

    # stash ctx for future restores (e.g. after restart)
    _ctx = ctx

    # first call: restore any persisted reminders
    if not _reminders and _load():
        n = await _restore(ctx.bot, ctx.chat_id)
        if n:
            _save()

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
        target = _target_from_time(time_str)
        label = f"at {time_str}"
    else:
        return {"error": "Provide either delay_minutes or time."}

    rid = _make_id()
    _schedule(ctx.bot, ctx.chat_id, rid, text, target)
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
