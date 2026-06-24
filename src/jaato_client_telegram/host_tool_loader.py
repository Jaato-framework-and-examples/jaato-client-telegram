"""Loader for dynamically-defined host tools.

The agent can extend the bot at runtime: it writes a tool *draft* into the
workspace, then calls the ``register_tool`` host tool. The bot (this code, which
runs UNCONFINED in the jaato-client-telegram process — not inside the
AppArmor-confined runner) validates the draft, asks the user to approve the
code, and on approval installs it into ``.jaato/host_tools/<name>.py`` and
registers it. The confined runner never writes ``.jaato`` itself.

A host-tool file defines exactly two module-level names:

    TOOL_SCHEMA = {
        "name": "crypto_price",                 # must equal the file stem
        "description": "Fetch a crypto price.",
        "parameters": {"type": "object", "properties": {...}},
    }

    async def execute(args: dict, ctx) -> dict:
        # ctx.bot   -> aiogram Bot      (talk to Telegram)
        # ctx.chat_id -> int            (the user's chat)
        ...
        return {"result": ...}          # or {"error": ...}

Loading a file EXECUTES its module body, so installs go through the approval
gate first. Files already in ``.jaato/host_tools/`` are trusted (bot-owned; the
confined runner cannot write there) and are loaded at startup without re-prompt.
"""

import asyncio
import importlib.util
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


# --- Single-poller-safe user prompts -----------------------------------------
# Pending ctx.ask()/ask_user() requests: request_id -> Future[int] (chosen option
# index). This registry lives in the bot process, where the dynamic-tool executors
# AND the main bot's callback_query router both run — so the ONE getUpdates poll
# Telegram allows (owned by the main bot) can resolve a tool's await. A tool must
# NEVER run its own poller (bot.get_updates / start_polling): two pollers on one
# token => TelegramConflictError and the whole bot stops receiving messages.
_HOST_CB_PREFIX = "host:"
_PENDING_ASKS: "dict[str, asyncio.Future[int]]" = {}


def resolve_host_ask(callback_data: str) -> bool:
    """Resolve a pending ask from a ``host:<id>:<index>`` callback. Returns True
    iff it matched a live request. Called by the main bot's callback_query router
    (the single poller) — this is what lets a tool receive a button tap without
    polling itself."""
    if not callback_data or not callback_data.startswith(_HOST_CB_PREFIX):
        return False
    try:
        _, req_id, idx = callback_data.split(":", 2)
        index = int(idx)
    except (ValueError, AttributeError):
        return False
    fut = _PENDING_ASKS.get(req_id)
    if fut is not None and not fut.done():
        fut.set_result(index)
        return True
    return False


async def ask_user(
    bot: Any, chat_id: int, text: str, options: list[str], timeout: float = 300.0,
) -> "str | None":
    """Send a single-choice question with inline buttons and AWAIT the answer —
    WITHOUT polling. The main bot's single getUpdates poll routes the button tap
    back here and resolves the await. Returns the chosen option string, or None on
    timeout. Use this (or ``ctx.ask``) from a tool or from an in-process server a
    tool starts. NEVER call ``bot.get_updates`` / start a second poller instead —
    that conflicts with the main bot."""
    if not options:
        raise ValueError("ask_user requires at least one option")
    req_id = uuid.uuid4().hex[:12]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(opt)[:60], callback_data=f"{_HOST_CB_PREFIX}{req_id}:{i}")]
        for i, opt in enumerate(options)
    ])
    fut: "asyncio.Future[int]" = asyncio.get_running_loop().create_future()
    _PENDING_ASKS[req_id] = fut
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        index = await asyncio.wait_for(fut, timeout=timeout)
        return options[index] if 0 <= index < len(options) else None
    except asyncio.TimeoutError:
        return None
    finally:
        _PENDING_ASKS.pop(req_id, None)


@dataclass
class ToolContext:
    """Runtime context handed to a dynamic tool's ``execute(args, ctx)``."""
    bot: Any
    chat_id: int

    async def ask(self, text: str, options: list[str], timeout: float = 300.0) -> "str | None":
        """Ask the user a single-choice question (inline buttons) and await their
        answer, routed through the main bot's single poll — NO polling of your own.
        Returns the chosen option string, or None on timeout. Set a matching long
        ``"timeout"`` (ms) in your tool's TOOL_SCHEMA so the runner waits for the
        human rather than giving up at the 30s default."""
        return await ask_user(self.bot, self.chat_id, text, options, timeout)


def validate_name(name: str) -> None:
    """Tool names are lowercase identifiers (also used as the file stem)."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"invalid tool name {name!r}: use lowercase letters, digits and "
            f"underscores, starting with a letter (2-41 chars)"
        )


def load_tool_file(path: Path) -> tuple[dict, Callable[..., Awaitable[Any]]]:
    """Import a host-tool file and return ``(schema, execute_fn)``.

    Raises ``ValueError`` if the file does not match the contract. NOTE: this
    executes the module body — only call it on trusted/approved files.
    """
    spec = importlib.util.spec_from_file_location(f"host_tool__{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{path.name}: could not load module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    schema = getattr(module, "TOOL_SCHEMA", None)
    execute = getattr(module, "execute", None)

    if not isinstance(schema, dict):
        raise ValueError(f"{path.name}: missing module-level TOOL_SCHEMA dict")
    name = schema.get("name")
    if not name or "parameters" not in schema:
        raise ValueError(f"{path.name}: TOOL_SCHEMA needs 'name' and 'parameters'")
    validate_name(name)
    if name != path.stem:
        raise ValueError(
            f"{path.name}: TOOL_SCHEMA name {name!r} must match filename {path.stem!r}"
        )
    if not callable(execute):
        raise ValueError(f"{path.name}: missing 'async def execute(args, ctx)'")

    return schema, execute


def make_executor(
    execute_fn: Callable[..., Awaitable[Any]], bot: Any, chat_id: int,
) -> Callable[[dict], Awaitable[dict]]:
    """Wrap a tool's ``execute(args, ctx)`` into the transport's ``(args)->dict``."""
    ctx = ToolContext(bot=bot, chat_id=chat_id)

    async def executor(args: dict) -> dict:
        try:
            result = await execute_fn(args or {}, ctx)
        except Exception as e:  # noqa: BLE001 — tool boundary
            logger.exception("dynamic host tool execution failed")
            return {"error": str(e)}
        return result if isinstance(result, dict) else {"result": result}

    return executor


def load_all_tools(host_tools_dir: Path) -> dict[str, dict]:
    """Load every ``*.py`` in ``host_tools_dir`` → ``{name: {schema, execute}}``.

    Invalid files are skipped with a warning so one bad tool never blocks the
    bot. Returns an empty dict if the directory does not exist.
    """
    tools: dict[str, dict] = {}
    if not host_tools_dir.is_dir():
        return tools
    for path in sorted(host_tools_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            schema, execute = load_tool_file(path)
        except Exception as e:  # noqa: BLE001 — skip bad files
            logger.warning("Skipping invalid host tool %s: %s", path.name, e)
            continue
        tools[schema["name"]] = {"schema": schema, "execute": execute}
        logger.info("Loaded dynamic host tool %r from %s", schema["name"], path.name)
    return tools
