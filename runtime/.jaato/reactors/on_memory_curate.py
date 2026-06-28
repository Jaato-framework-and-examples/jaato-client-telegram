"""Reactor: deterministically drain this workspace's raw memories to curated.

Fires on the bot's main agent completing a turn (``source_agent ==
'telegram_chat'`` — the bot starts sessions with ``--agent telegram_chat``, so
its ``agent.completed`` carries that id, NOT the default ``"main"``).

Why this exists: the memory plugin surfaces only the CURATED store
(``.jaato/memories/curated.jsonl``) as prompt enrichment; freshly stored
memories land in the RAW queue (``memories/raw/``) and stay there until a
curator promotes them. The reference design uses an LLM ``memory-advisor``
agent for that, spawned per completion — overkill (and a per-turn LLM cost) for
a single-user chat bot. Instead we promote raw -> curated deterministically,
using the plugin's OWN transition (``MemoryStore.update`` moves a raw memory
marked ``validated`` into the curated store). No LLM, no extra session, no cost;
the next session's enrichment then surfaces the memories, so the bot remembers
across sessions.

Runs in the (unconfined) daemon reactor engine, so it can read the raw queue and
write ``curated.jsonl`` directly.
"""

import dataclasses
import logging

from shared.plugins.memory.models import MATURITY_VALIDATED
from shared.plugins.memory.storage import MemoryStore

log = logging.getLogger("reactor.memory_curate")


def execute(params, event, ctx):
    workspace = getattr(ctx.server, "_workspace_path", None)
    if not workspace:
        return

    store = MemoryStore(f"{workspace}/.jaato/memories")
    raw = store.list_raw()
    if not raw:
        return

    for memory in raw:
        store.update(dataclasses.replace(memory, maturity=MATURITY_VALIDATED))

    log.info("memory_curate: promoted %d raw -> curated (%s)", len(raw), workspace)
