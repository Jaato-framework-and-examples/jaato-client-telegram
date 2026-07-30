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
import contextvars
import importlib
import logging
import re
import sys
import types
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


# ---- delivered-content capture (make a tool's chat sends visible to the model) ----
# A model-authored host tool reaches the model ONLY through what its execute()
# RETURNS (the daemon JSON-encodes the return value as the tool result). A tool that
# renders straight to Telegram — ctx.bot.send_message(...) — and returns just a status
# (or None) leaves the model blind to what it delivered, so it cannot answer follow-ups
# about that content. That correctness must not depend on every model-authored tool
# remembering to also return what it sent.
#
# This contextvar closes the gap at the harness level: make_executor sets a fresh
# recorder around each execute() call; ThreadAwareBot appends the visible text of every
# send that lands in the tool's chat (see thread_bot.record_delivery call); make_executor
# then folds the captured lines into the returned dict under `already_shown_to_user`, so
# the model always sees what its tool pushed — whatever the author remembered to return.
# It is task-scoped (contextvars follow the awaited execute() coroutine), so concurrent
# tool calls in different chats never cross-contaminate. Set only on the dynamic-tool
# path — the built-ins (send_to_telegram/show_image) are bot-authored and already return
# proper results, so no recorder is active for them and record_delivery no-ops.
_DELIVERY_RECORDER: "contextvars.ContextVar[list[str] | None]" = contextvars.ContextVar(
    "host_tool_delivery_recorder", default=None
)


def record_delivery(text: str) -> None:
    """Append user-visible text a host tool just delivered to its chat to the active
    per-call recorder. No-op when no recorder is set (outside a dynamic tool's execute,
    e.g. a built-in executor's sends) or when ``text`` is empty."""
    recorder = _DELIVERY_RECORDER.get()
    if recorder is not None and text:
        recorder.append(text)


# ---- render-flush coordination (narration before an out-of-band tool prompt) ----
# A host tool's ask_user()/ctx.ask() sends its prompt IMMEDIATELY via bot.send_message,
# bypassing the renderer's throttled narration — so the prompt can reach Telegram before
# the narration message even exists (prompt-before-narration). The renderer registers a
# per-chat flush hook while it streams a turn; ask_user calls it before sending, so the
# buffered narration lands FIRST. (The renderer's IN-STREAM permission path already
# flushes; this closes the OUT-OF-BAND tool-prompt gap.)
_FLUSH_HOOKS: "dict[int, Callable[[], Awaitable[None]]]" = {}


def register_flush_hook(chat_id: int, hook: "Callable[[], Awaitable[None]]") -> None:
    """Renderer: while streaming a chat's turn, register a coro that flushes its
    pending narration. Overwrites any prior hook for the chat (last turn wins)."""
    _FLUSH_HOOKS[chat_id] = hook


def unregister_flush_hook(chat_id: int) -> None:
    _FLUSH_HOOKS.pop(chat_id, None)


async def flush_before_prompt(chat_id: int) -> None:
    """Flush the chat's pending narration (if a renderer is streaming its turn) so an
    out-of-band prompt sends AFTER it. No-op when nothing is registered."""
    hook = _FLUSH_HOOKS.get(chat_id)
    if hook is None:
        return
    try:
        await hook()
    except Exception:  # noqa: BLE001 — a flush fault must never block the prompt
        logger.debug("flush_before_prompt failed for chat %d", chat_id, exc_info=True)


async def ask_user(
    bot: Any,
    chat_id: int,
    text: str,
    options: list[str],
    timeout: float = 300.0,
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=str(opt)[:60], callback_data=f"{_HOST_CB_PREFIX}{req_id}:{i}"
                )
            ]
            for i, opt in enumerate(options)
        ]
    )
    fut: "asyncio.Future[int]" = asyncio.get_running_loop().create_future()
    _PENDING_ASKS[req_id] = fut
    try:
        # NOTE: narration is flushed for us — ask_user's `bot` is the tool's
        # ThreadAwareBot, whose send_* wrapper calls flush_before_prompt before every
        # send (ONE flush point for all out-of-band tool sends). No flush here.
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
    # Injected by the bot: submit an agent-facing event turn to the chat's pump.
    # None when no pump is wired (feature off) — ctx.wake() then no-ops.
    wake_fn: "Callable[[int, str], None] | None" = None
    # The session's workspace root ("" if unconfigured). A tool that stages a file
    # for the agent (e.g. install_tool writing the verified draft to
    # tool_drafts/<name>.py) writes under here.
    workspace: str = ""
    # The bot-owned host_tools_dir where INSTALLED tools live ("" if unconfigured).
    # A tool that reads installed tool source (e.g. share_tool contributing one to
    # the store) reads from here.
    host_tools_dir: str = ""
    # Injected by the bot: register / deregister a wake BINDING for THIS session over
    # the live WS (session.bind_wake / unbind_wake). None when unavailable —
    # ctx.bind_wake()/unbind_wake() then return outcome="disabled".
    bind_fn: "Callable[[int, str, list], Awaitable[dict]] | None" = None
    unbind_fn: "Callable[[int, str], Awaitable[dict]] | None" = None

    async def ask(self, text: str, options: list[str], timeout: float = 300.0) -> "str | None":
        """Ask the user a single-choice question (inline buttons) and await their
        answer, routed through the main bot's single poll — NO polling of your own.
        Returns the chosen option string, or None on timeout. Set a matching long
        ``"timeout"`` (ms) in your tool's TOOL_SCHEMA so the runner waits for the
        human rather than giving up at the 30s default."""
        return await ask_user(self.bot, self.chat_id, text, options, timeout)

    async def wake(self, text: str) -> None:
        """Make the MODEL act on an event this tool is raising (e.g. a reminder
        firing) — even if the session has gone idle since. Delivers ``text`` as a
        new agent turn: resumes/re-attaches the session if needed, and if the user
        happens to have a turn in flight it WAITS for that to finish rather than
        interrupting it. Returns immediately after submitting (does not await the
        model's reply). Status-agnostic: you do NOT need to know the session state.
        No-ops if the bot didn't wire a pump."""
        if self.wake_fn is None:
            return
        self.wake_fn(self.chat_id, text)

    async def bind_wake(self, wake_ref: str, trust_keys: list) -> dict:
        """Register a wake BINDING for this session: an external signer holding the
        private half of a key whose PUBLIC half is in ``trust_keys`` (PEM) can later
        wake this session by POSTing ``wake_ref`` + a signature to the daemon's wake
        ingress. ``wake_ref`` is YOURS to choose — a routing handle you share with
        your caller (e.g. ``"github-pr:owner/repo#42"``). Owner-guarded upsert:
        re-call to refresh the key set (rotation). Returns ``{outcome, expires_at,
        detail}`` — ``outcome`` ∈ ``ok`` / ``unauthorized`` / ``malformed_key`` /
        ``too_many_keys`` / ``no_keys`` / ``no_session``. Returns
        ``outcome="disabled"`` if the bot didn't wire wake binding."""
        if self.bind_fn is None:
            return {"outcome": "disabled", "detail": "wake binding not wired"}
        return await self.bind_fn(self.chat_id, wake_ref, list(trust_keys))

    async def unbind_wake(self, wake_ref: str) -> dict:
        """Remove a wake binding you created (owner-guarded). Call when the matter
        ends (e.g. your PR merged/closed). The daemon's TTL also expires a forgotten
        binding. Returns ``{outcome, detail}`` (same disabled/no_session semantics)."""
        if self.unbind_fn is None:
            return {"outcome": "disabled", "detail": "wake binding not wired"}
        return await self.unbind_fn(self.chat_id, wake_ref)


def validate_name(name: str) -> None:
    """Tool names are lowercase identifiers (also used as the file stem)."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"invalid tool name {name!r}: use lowercase letters, digits and "
            f"underscores, starting with a letter (2-41 chars)"
        )


def tools_venv_site_packages(venv: Path) -> Path:
    """Expected ``site-packages`` of the tools venv for the CURRENT (bot)
    interpreter: ``<venv>/lib/pythonX.Y/site-packages``.

    Returned whether or not it exists yet — the confined runner creates the venv
    on its first ``pip install``, and putting the path on ``sys.path`` early means
    a later install resolves without a bot restart (Python skips absent path
    entries at import time). The venv MUST be built with the bot's Python for the
    in-process import to be ABI-compatible; a mismatched version yields a path
    that never exists (feature stays off — no silent fallback)."""
    return (
        venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    )


def load_tool_file(path: Path) -> tuple[dict, Callable[..., Awaitable[Any]]]:
    """Import a host-tool file and return ``(schema, execute_fn)``.

    Raises ``ValueError`` if the file does not match the contract. NOTE: this
    executes the module body — only call it on trusted/approved files.
    """
    # Compile the source DIRECTLY — do not go through importlib's __pycache__.
    # importlib validates the bytecode cache by source mtime at 1-second
    # granularity, so re-registering a modified tool (overwrite + reload within
    # the same second, as register_tool does) would run STALE bytecode — the new
    # version would only take effect on a later/new session. compile()+exec()
    # reads the current source every time, so a re-registration takes effect
    # immediately.
    try:
        source = path.read_text()
    except OSError as e:
        raise ValueError(f"{path.name}: could not read file: {e}") from e
    module = types.ModuleType(f"host_tool__{path.stem}")
    module.__file__ = str(path)
    try:
        exec(
            compile(source, str(path), "exec"), module.__dict__
        )  # noqa: S102 — trusted, user-approved file
    except Exception as e:  # noqa: BLE001 — surface any load error as ValueError
        raise ValueError(f"{path.name}: failed to load: {e}") from e

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
    execute_fn: Callable[..., Awaitable[Any]],
    bot: Any,
    chat_id: int,
    wake: "Callable[[int, str], None] | None" = None,
    workspace: str = "",
    host_tools_dir: str = "",
    bind_fn: "Callable[[int, str, list], Awaitable[dict]] | None" = None,
    unbind_fn: "Callable[[int, str], Awaitable[dict]] | None" = None,
) -> Callable[[dict], Awaitable[dict]]:
    """Wrap a tool's ``execute(args, ctx)`` into the transport's ``(args)->dict``."""
    ctx = ToolContext(
        bot=bot,
        chat_id=chat_id,
        wake_fn=wake,
        workspace=workspace,
        host_tools_dir=host_tools_dir,
        bind_fn=bind_fn,
        unbind_fn=unbind_fn,
    )

    async def executor(args: dict) -> dict:
        # A tool may import a dependency the confined runner just pip-installed
        # into the workspace tools venv (on sys.path). Drop importlib's finder
        # caches so a call-time `import X` sees a package added AFTER the venv dir
        # was first scanned (empty) at startup — the "install then use it now"
        # case, e.g. moon_phase importing skyfield inside execute().
        importlib.invalidate_caches()
        # Capture everything the tool renders to its chat during this call so the
        # model sees it even when the tool returns only a status (see _DELIVERY_RECORDER).
        recorder: list[str] = []
        token = _DELIVERY_RECORDER.set(recorder)
        try:
            result = await execute_fn(args or {}, ctx)
        except Exception as e:  # noqa: BLE001 — tool boundary
            logger.exception("dynamic host tool execution failed")
            return {"error": str(e)}
        finally:
            _DELIVERY_RECORDER.reset(token)
        result = result if isinstance(result, dict) else {"result": result}
        if recorder:
            # Fold in what was delivered to the user, under a key that tells the model
            # this content is ALREADY shown (so it references it for follow-ups rather
            # than resending). setdefault: never clobber a value the tool set itself.
            result.setdefault("already_shown_to_user", list(recorder))
        return result

    return executor


# Provenance marker for tools a user installed at runtime via register_tool.
# The runner-tier model only reliably reads name/description/parameters from a
# tool schema — category/timeout/auto_approve are stripped server-side and never
# reach it. So a just-installed tool is indistinguishable in the flat tool array
# from one present at bootstrap, and the model confabulates "built-in". Putting
# provenance in the DESCRIPTION is the only channel the model sees every turn.
USER_INSTALLED_TAG = "[user-installed]"


def mark_user_installed(schema: dict) -> dict:
    """Return a copy of ``schema`` whose description is prefixed with
    ``USER_INSTALLED_TAG`` so the model can always tell a user-installed host tool
    from a bootstrap/built-in one. Idempotent: a schema already tagged is returned
    unchanged."""
    desc = schema.get("description", "")
    if desc.startswith(USER_INSTALLED_TAG):
        return schema
    marked = dict(schema)
    marked["description"] = (
        f"{USER_INSTALLED_TAG} (custom tool created at runtime via register_tool "
        f"— not a built-in) {desc}".rstrip()
    )
    return marked


def load_all_tools(host_tools_dir: Path) -> dict[str, dict]:
    """Load every ``*.py`` in ``host_tools_dir`` → ``{name: {schema, execute}}``.

    Invalid files are skipped with a warning so one bad tool never blocks the
    bot. Returns an empty dict if the directory does not exist.
    """
    tools: dict[str, dict] = {}
    if not host_tools_dir.is_dir():
        return tools
    # A tool's deps may have just been pip-installed into the tools venv (on
    # sys.path) by the confined runner. Drop importlib's finder caches so a tool
    # importing that fresh package resolves on this (re)load instead of failing
    # until the next bot restart.
    importlib.invalidate_caches()
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
