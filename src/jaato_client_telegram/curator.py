"""Consolidate the bot's raw memory queue after a chat goes idle.

Port of jaato-escriba's curator: a separate, memory-only session that judges what
the chat model stored **raw** — validate / dismiss / fix-in-place — so the next
session wakes up knowing it. A raw memory is reachable by no tag search and is
injected at no wake, so until it is judged it does not exist for the next session.

Runs on **idle-detach** (the "conversation ended" boundary), in the background.
Replaces the deterministic promote-all drain that used to fire on every
``store_memory`` (which validated everything blindly — no dedup, no dismissal of
conversational noise, no fixing a bad description).

The curator dials over the WS facade as a headless ``ClientType.API`` session,
from the ISOLATED ``.jaato-judge`` config_root (so the chat's ``subagent`` plugin
can't offer it as a spawnable profile), but with ``workspace_path`` = the bot's
workspace, so its memory plugin resolves the default ``.jaato/memories`` to the
SAME store the bot writes to.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from jaato_sdk import WSRecoveryClient

log = logging.getLogger(__name__)

CURATOR_PROFILE = "openrouter/curator"
CURATOR_AGENT = "curator"
#: The single prompt that wakes the curator. Its rules — eight at a time, scope
#: project, validate/dismiss/fix — live in the persona, not here.
DRAIN = "Juzga lo que haya en crudo."
#: Client-side safety net; the curator is also budget-capped server-side
#: (turns/usd/tool_calls + degrade abort), so it terminates on its own.
CURATOR_TIMEOUT = 240.0


def raw_memory_count(workspace: Path) -> int:
    """How many RAW (un-judged) memories the bot's store holds — the gate for
    running the curator at all (no point waking an LLM for an empty queue).

    Reads the same workspace store the memory plugin uses (`.jaato/memories`).
    Returns 0 if the server's memory package isn't importable in the bot env, so
    the bot simply skips curation rather than failing."""
    try:
        from shared.plugins.memory.storage import MemoryStore
    except Exception:
        log.debug("curator: shared.plugins.memory unavailable — skipping", exc_info=True)
        return 0
    try:
        store = MemoryStore(f"{str(workspace).rstrip('/')}/.jaato/memories")
        return len(store.list_raw())
    except Exception:  # noqa: BLE001 — store read boundary
        log.debug("curator: could not read the raw queue", exc_info=True)
        return 0


async def run_curator(conn: Dict[str, Any]) -> None:
    """Open a curator session and drain the raw memory queue.

    ``.ask`` (not ``.complete``): the curator declares NO completion schema — it
    is a faculty, not a service, so there is no ``signal_completion``. It retrieves
    raw memories eight at a time and judges each; its turn ends when the queue is
    empty or its budget caps, and ``.ask`` returns then.
    """
    async with WSRecoveryClient.session(
        profile=CURATOR_PROFILE, agent=CURATOR_AGENT, **conn
    ) as curator:
        summary = await curator.ask(DRAIN, timeout=CURATOR_TIMEOUT)
    log.info(
        "curator: drain finished — %s",
        (summary or "").strip()[:200] or "(no summary)",
    )
