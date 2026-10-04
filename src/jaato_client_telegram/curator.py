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
    """How many RAW (un-judged) memories the bot's store holds -- the gate for
    running the curator at all (no point waking an LLM for an empty queue).

    Counts ``<workspace>/.jaato/memories/raw/*.json`` the way the framework's own
    raw store enumerates it (``list_all``): one file per raw memory, ``.json``
    only -- the atomic writer's ``.tmp`` files are not memories. An ABSENT
    directory is an empty queue, not an error: the store creates ``raw/`` lazily
    on its first write, so a workspace that never stored a raw memory has none.

    It reads the files instead of importing the framework's store because the bot
    runs from its own venv with only ``jaato-sdk``. That import used to be here,
    as ``shared.plugins.memory.storage`` -- a jaato-server INTERNAL, under its
    pre-1.0 name, so it had been dead since 1.0 behind ``except Exception:
    return 0`` at DEBUG: a broken gate read exactly like an empty queue.
    ``test_curator_raw_count.py`` now holds this count equal to ``list_raw()``
    wherever the framework is installed, so a change to the store's layout fails
    a test instead of silently reading as "nothing to curate".

    A directory that exists but cannot be read is NOT an empty queue: it is
    logged at WARNING and skipped for this sweep -- visible (after moving the
    bot to its own account, a wrong owner shows up exactly here) without
    disturbing the chats. It over-counts a corrupt ``.json`` that ``list_raw``
    would skip; for a gate that is the safe direction -- the curator wakes and
    finds nothing, rather than never waking.
    """
    raw_dir = Path(workspace) / ".jaato" / "memories" / "raw"
    try:
        return sum(1 for entry in raw_dir.iterdir() if entry.suffix == ".json")
    except FileNotFoundError:
        return 0
    except OSError:
        log.warning("curator: cannot read the raw memory queue at %s -- "
                    "skipping curation this sweep", raw_dir, exc_info=True)
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
