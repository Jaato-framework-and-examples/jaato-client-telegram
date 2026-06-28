# Memory continuity: remembering across sessions

The bot's agent uses jaato's **memory plugin** (enabled in the `telegram_chat`
profile) to persist learnings and user facts across sessions — so it can recall
"your favorite city is Lisbon" in a conversation days later, independent of the
session re-attach mechanism (see `docs/design/session-lifecycle.md`).

## How the memory plugin scopes and surfaces memories

- **Scope = the workspace.** Memories live under `<workspace>/.jaato/memories/`.
  The bot uses **one** workspace (`runtime`) for every chat, so for this
  single-user bot memory is naturally global and continuous across all chats and
  sessions. (Multi-user isolation would instead need a per-chat workspace — a
  chat-id *tag* does not isolate a shared store.)
- **Two-stage store.** New memories land in a **raw** queue
  (`memories/raw/<id>.json`). Prompt enrichment — the "💡 you have memories
  about…" hint injected at session start — is built from the **curated** store
  only (`memories/curated.jsonl`). Raw is a pending queue and is *not* surfaced
  (`jaato-server shared/plugins/memory/plugin.py:246`).
- **Consequence:** without something draining raw → curated, the agent stores
  memories but never sees them again, and greets every session as a "fresh
  start". That is the gap this feature closes.

## The deterministic curation reactor

The reference design drains raw → curated with an LLM `memory-advisor` agent
spawned on every completion — overkill and a per-turn LLM cost for a single-user
chat bot. Instead we use a **deterministic reactor**:

- `runtime/.jaato/reactors.json` — fires on `agent.completed` where
  `source_agent == 'telegram_chat'`. (The bot starts sessions with
  `--agent telegram_chat`, so its completion event carries that id, **not** the
  default `"main"` the reference assumes — matching `'main'` would never fire.)
- `runtime/.jaato/reactors/on_memory_curate.py` — promotes every raw memory to
  `validated` via the plugin's own transition (`MemoryStore.update`: a raw
  memory marked validated moves into the curated store). Runs in the unconfined
  daemon reactor engine, so it can write `curated.jsonl`. **No LLM, no spawned
  session, no per-turn cost**, and no loop-guard needed (it never creates a
  session). The next session's enrichment then surfaces the memories.

Trade-off: it promotes raw memories without quality judgment. Acceptable here —
the agent only stores what it deems worth remembering. If LLM-grade curation
(promote good / dismiss junk) is ever wanted, swap in the `memory-advisor`
reactor (`jaato-knowledge-manager/.jaato.example/`), at the per-turn LLM cost.

## One-time backfill

Existing raw memories predating the reactor were promoted once with the same
transition (`MemoryStore.update(..., validated)`), so continuity worked
immediately rather than only for memories created after wiring.

## Prerequisites / notes

- The daemon must run the **premium reactor engine** with workspace-tier reactor
  discovery (the shared daemon already composes a `premium-reactor` AppArmor
  fragment for active sessions). Workspace reactors are read at session spawn, so
  a newly added reactor takes effect on the next freshly-spawned session.
- `curated.jsonl` and `memories/raw/` are **transient state** and stay gitignored;
  only the reactor config (`reactors.json`, `reactors/`) is versioned.
