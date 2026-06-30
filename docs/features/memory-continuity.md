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

## Client-side deterministic curation (on `tool.call_end`)

The reference design drains raw → curated with an LLM `memory-advisor` agent
spawned on every completion — overkill and a per-turn LLM cost for a single-user
chat bot. **Earlier** this bot used a premium daemon **reactor** to drain
deterministically; that coupled the deployment to premium's reactor engine. It
now drains **client-side** instead, removing the premium dependency:

- `SessionPool` subscribes to `EventType.TOOL_CALL_END`. When a memory-write tool
  (`store_memory` / `memory` / `update_memory`) completes **successfully**, it
  runs the exact same deterministic transition the reactor did — `MemoryStore`
  `list_raw()` → promote each to `validated` (`MemoryStore.update`), which moves
  it into the curated store. **No LLM, no spawned session, no per-turn cost.**
  The next session's enrichment then surfaces the memories.
- The drain logic is server-core (`shared.plugins.memory`), imported lazily and
  guarded, so the bot degrades gracefully (skips curation) if the package or
  workspace is absent. The subscription rides the recovery client's registry, so
  it survives reconnects.

Scope: this drains the **workspace** store (`<workspace>/.jaato/memories`), which
is exactly what the old reactor covered. Trade-off (unchanged): it promotes raw
memories without quality judgment — acceptable here, since the agent only stores
what it deems worth remembering.

> **Known design question (with Advisor):** `store_memory(scope=universal)` writes
> raw memories directly into the **HOME** store (`~/.jaato/memories/raw/`), which
> this drain deliberately does **not** touch — raw-in-home is considered wrong
> (the home/global tier should be curated-only; promotion to home should be a
> deliberate curation decision, not a raw write). Pending Advisor's analysis of
> the memory-plugin scope/tiering.

## Prerequisites / notes

- No premium reactor engine required — curation is client-side. The memory plugin
  itself (server-core) provides the raw/curated stores and the `MemoryStore`
  transition.
- `curated.jsonl` and `memories/raw/` are **transient state** and stay gitignored.
