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

import importlib.util
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@dataclass
class ToolContext:
    """Runtime context handed to a dynamic tool's ``execute(args, ctx)``."""
    bot: Any
    chat_id: int


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
