# jaato-client-telegram — jaato client peer-review

**Date:** 2026-06-20 · **Reviewer:** `enphase-review` peer (same pass as enphase_monitoring)
**Baseline:** current jaato at `../jaato`; source-of-truth `jaato-sdk-client/SKILL.md`
**Tools:** `jaato-doctor` + `jaato-scaffold` (run from `/tmp/jaato-test`, which has
`jaato_sdk` 0.14.6 + `jaato-server` editable), plus live event-schema introspection.

Targets: `src/jaato_client_telegram/` (transport, session pool, handlers, renderer,
permissions), the `.jaato/` workspace, `.env`, `pyproject.toml`.

## TL;DR

Architecturally **healthier than enphase_monitoring**. This client does **not**
use `IPCClient`; it speaks the jaato **WebSocket** server protocol directly via a
hand-rolled `WSTransport` (per-chat connection + session), using the SDK only for
event (de)serialization + event types. That WS server + protocol is **still
current** (`server/websocket.py`, `command_router.py` `session.new`), and almost
every event contract the client uses still matches the installed SDK — so it
largely **runs**.

The cost of hand-rolling the protocol is **silent drift**: one event schema
(`ClarificationResponseRequest`) changed and the client now sends a dropped field
with **no error**. Plus the usual staleness (no profile/agent, dead env knob,
loose SDK pin) and two hygiene issues (a 163 MB venv was committed to git; one
permission field silently dropped).

`jaato-doctor`: 0 fail / 1 warn. `jaato-scaffold validate .`: vacuously clean (no
profiles). Daemon reachable, HOME matches.

---

## A. BROKEN vs current APIs

Evidence: live `model_fields` introspection of every event class the client
constructs/reads, cross-checked against call-sites.

1. **`ClarificationResponseRequest` schema drift — SILENT failure.**
   `session_pool.py:158` builds `ClarificationResponseRequest(request_id=…,
   responses=<dict>)`. The current model fields are
   **`request_id, question_index, response`** (singular) — the server now matches
   answers by 1-based `question_index` (`shared/plugins/clarification/models.py`).
   Pydantic **silently ignores** the unknown `responses=` kwarg and emits
   `question_index=0, response=""` (verified live: constructs without error). So a
   user's clarification answer would reach the server as an **empty response to
   question 0** — no crash, no log, just wrong. This is exactly the silent-ignore
   class the SKILL warns about, and the reason hand-rolled protocol clients rot
   quietly.
   *Severity note:* currently **latent** — `respond_to_clarification` has **no
   caller** (the renderer never surfaces `CLARIFICATION_*` events; see B2), so the
   bug can't fire today. But the method is broken-by-construction the moment
   clarification is wired.

**Everything else the client sends/reads still matches the SDK** (checked:
`SendMessageRequest`, `PermissionResponseRequest`, `ClientConfigRequest`,
`StopRequest`, `SessionInfoEvent`, `ToolExecuteRequestEvent`,
`ToolExecuteResultEvent`, `ToolsRegisterClientRequest`, `CommandRequest`,
`StageFilesRequest`, `StagedFileSpec`, `ConnectedEvent`, `ErrorEvent`,
`PermissionRequestedEvent`). The renderer matches event types by **string**
(`"agent.output"`/`"AGENT_OUTPUT"`) with defensive `getattr`, so enum/name changes
degrade gracefully rather than crash — robust, but see B5.

---

## B. STALE vs current best-practice

1. **No `.jaato/profiles/` and no agent persona — sessions use the framework
   default.** `transport.create_session` sends `CommandRequest(command="session.new",
   args=[])` — **empty args**. The server's `session.new` explicitly accepts
   `--profile <name>` / `--agent <name>` / `key=value` agent-params, or an inline
   `payload['spec']` profile dict (`command_router.py:275`). Current best-practice
   for a chat bot: ship a `telegram_chat` **agent persona** (`.jaato/agents/*.md`)
   + a **profile** (chat-tuned: plugins, gc, max_turns, the presentation already
   sent via `ClientConfigRequest`) and pass `--agent/--profile` (or `payload.spec`)
   at session.new. Right now the bot's persona/tooling is whatever the daemon
   defaults to — undeclared and unversioned.

2. **Clarification flow is unimplemented end-to-end.** The renderer surfaces no
   `CLARIFICATION_REQUESTED/QUESTION/BATCH` events to Telegram, and the only
   responder (`respond_to_clarification`) is dead **and** broken (A1). Either
   implement it (surface the question as a Telegram prompt → respond with the
   correct `question_index`+`response` schema) or delete the dead method. Today, if
   an agent asks a clarifying question, the user never sees it and the turn stalls.

3. **`AI_USE_CHAT_FUNCTIONS=1` in `.env` is a dead knob** — 0 reads in the
   framework (same finding as enphase_monitoring). Remove. (`JAATO_PROVIDER=zhipuai`
   / `MODEL_NAME=glm-5-turbo` are valid — `glm-5-turbo` is a known model. No
   `JAATO_PROFILE_SET` set, consistent with having no profiles.)

4. **`pyproject.toml` pins `jaato-sdk>=0.1.0`** — a floor four-plus minor
   versions below the schemas this client actually depends on (the clarification
   model, the WS event set). A `0.1.0` resolve would not even have today's event
   shapes. Pin a current floor (e.g. `>=0.14`) or the local editable SDK, matching
   what's installed (0.14.6).

5. **Defensive `getattr`/dual-string event matching in `renderer.py`** is pragmatic
   but anti-deterministic (per the repo owner's CLAUDE.md rules) and is *precisely*
   what let A1's silent drift stay invisible. Where the event schema is stable,
   prefer the typed `EventType` enum + real fields so drift fails loudly. Lower
   priority (rendering layer), but the philosophy is the same root cause as A1.

6. **`respond_to_permission` silently drops `edited_arguments`.** The method
   accepts `edited_arguments` (`session_pool.py:144`) but builds
   `PermissionResponseRequest(request_id=…, response=…)` without it — yet the SDK
   field exists (`…, response, edited_arguments`). Permission *edits* (user tweaks
   a tool's args before approving) never reach the server. Wire it through.

---

## C. HYGIENE

1. **A 163 MB virtualenv (`jaatotelegram/`) was committed to git** (43 tracked
   files; not covered by `.gitignore`, which only had `.venv`/`venv/`). Removed
   from disk + untracked + added to `.gitignore` during this pass. *(Already
   applied — staged as 43 deletions + the `.gitignore` edit.)* `.venv/` (41 MB,
   already-ignored) also removed from disk. Tooling now uses `/tmp/jaato-test`.

---

## D. Prioritized punch-list

### P0 — correctness
1. Fix or remove `ClarificationResponseRequest` usage: switch to
   `question_index` + singular `response`, **or** delete the dead
   `respond_to_clarification` if clarification won't be implemented now (A1/B2).

### P1 — current-patterns / correctness
2. Author a `telegram_chat` agent persona + profile under `.jaato/`; pass
   `--agent`/`--profile` (or `payload.spec`) at `session.new` instead of empty
   args (B1). Validate with `jaato-scaffold validate .`.
3. Implement the clarification UX (surface `CLARIFICATION_*` → Telegram → correct
   response schema), or consciously drop it (B2).
4. Forward `edited_arguments` in `respond_to_permission` (B6).
5. Repin `jaato-sdk` to a current floor / the local editable SDK (B4).

### P2 — hygiene
6. Remove `AI_USE_CHAT_FUNCTIONS` from `.env` (B3).
7. Tighten `renderer.py` event handling toward typed `EventType`/fields where the
   schema is stable, so future drift fails loudly (B5).
8. Commit the venv removal + `.gitignore` fix (C1).

---

## E. Implementation status — punch-list applied (2026-06-21)

All P0–P2 items were implemented (full clarification UX + renderer hardening per
the user's choices).

**Applied:**
- **P0.1 / P1.3 — full clarification UX.** New `clarification.py`
  (`ClarificationHandler`, mirrors `PermissionHandler`): handles the WS-native
  `ClarificationBatchEvent` (all questions at once), single-choice via inline
  buttons (`clar:` callbacks), multiple-choice / free-text via text reply, and
  submits a correct `ClarificationBatchResponseEvent` (1-based ordinals for
  choices, literal text for free-text). Wired into `renderer.py` (surface),
  `handlers/callbacks.py` (button answers), `handlers/private.py` (text answers
  routed *before* the per-user lock to avoid the deadlock), and `bot.py`
  (injected like `permission_handler`). The broken `respond_to_clarification`
  (`responses=` dict) is replaced.
- **P1.2 — profile + agent.** `.jaato/profiles/telegram_chat.yaml` (+
  `clarification` plugin so the agent can actually ask) and
  `.jaato/agents/telegram_chat.md`; passed at `session.new` via new
  config-driven `jaato_ws.profile` / `jaato_ws.agent` (no hardcoding — names live
  in config). `jaato-scaffold validate .` → clean.
- **P1.4** — `respond_to_permission` now forwards `edited_arguments`.
- **P1.5** — `pyproject` repinned `jaato-sdk>=0.14`.
- **P2.6** — removed `AI_USE_CHAT_FUNCTIONS` from `.env`.
- **P2.7 — renderer hardening.** Event-loop branches now compare typed
  `EventType` members (behaviour-preserving via the str-enum; SDK renames now
  fail loudly). This surfaced **dead code**: `file.generated` has no
  `EventType` and is never emitted by the server — flagged in-code (file
  delivery now flows via host tools / WORKSPACE_FILES).
- **C1 / P2.8** — the committed 163 MB `jaatotelegram/` venv removed + untracked
  + gitignored; `.venv` removed from disk.

**Validation evidence (from the venv where jaato is installed):**
- `jaato-scaffold validate .` → all profiles valid.
- `jaato-doctor --workspace . --env-file .env` → 0 fail / 1 warn.
- New `tests/test_clarification.py` → **5/5 pass** (UI build, ordinal callbacks,
  advance→done, batch-response wiring).
- All changed modules compile + import; str-enum equality verified load-bearing.

**Not verified (needs a live bot):** the end-to-end async clarification flow
(surface → button/text answer → batch submit → stream resumes) requires a running
Telegram bot + an agent that calls `request_clarification`; only the pure logic,
wiring, and imports are verified here.

**Pre-existing test breakage (NOT from this work):** `test_group.py`,
`test_workspace.py`, `test_connection_recovery.py`, and parts of
`test_client.py` reference symbols removed in the earlier IPC→WS migration
(`JaatoConfig`, `config.jaato`, `SessionInfo`, `workspace` module);
`test_filters.py`/`test_whitelist.py` fail under the newer aiogram (3.29) in
modules this work didn't touch. Worth a separate test-refresh pass.

---

## Appendix — verification commands

Run from the venv where jaato is installed (`/tmp/jaato-test` here):

- `/tmp/jaato-test/bin/jaato-doctor --workspace . --env-file .env` → 0 fail / 1 warn.
- `/tmp/jaato-test/bin/jaato-scaffold validate .` → vacuously clean (no profiles).
- `/tmp/jaato-test/bin/jaato-scaffold explain sets --workspace .` → *no .jaato/profiles/*.
- event-shape check:
  `/tmp/jaato-test/bin/python -c "from jaato_sdk.events import ClarificationResponseRequest as C; print(list(C.model_fields))"`
  → `['type','timestamp','request_id','question_index','response']` (not `responses`).
- server WS contract: `grep -n 'session.new' ../jaato/jaato-server/server/command_router.py`.
