# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone Telegram bot that bridges Telegram chats to a running **jaato** AI agent server. It is a *client*, not a plugin: all agent logic, tool execution, plugins, and permissions live in the jaato server. This process is purely an I/O surface — it translates Telegram messages into jaato session events and renders the streamed event responses back into Telegram messages.

The agent server is a separate project (`jaato-server`, reachable in sibling dirs under `../`). When behavior depends on what the server does with an event, the source of truth is there, not here.

## Commands

```bash
pip install -e ".[dev]"                 # install with dev deps (pytest, black, ruff, mypy)

pytest tests/                           # run all unit tests (no server required)
pytest tests/test_client.py::TestResponseRenderer::test_split_short_text -v   # single test
pytest tests/ --cov=src/jaato_client_telegram --cov-report=html               # with coverage

black src/                              # format (line-length 100)
ruff check src/                         # lint (rules: E, F, I, N, W)
mypy src/                               # type-check (disallow_untyped_defs = true)

python -m jaato_client_telegram         # run the bot (or: jaato-tg)
jaato-tg --config <path> --whitelist <path>
./start.sh                              # guided first-run: scaffolds config, checks token + server port
```

Unit tests run fully offline (they mock the transport/server). Integration testing is manual and needs a live jaato server + a real bot token — see `TESTING.md`.

## Transport: WebSocket, not IPC

**Important drift to know:** prose in `README.md` and several `docs/` pages still describe a Unix-socket "jaato-sdk IPC" connection (`/tmp/jaato.sock`, `socket_path`). The code does **not** do this. The live transport is a hand-rolled **WebSocket** client (`transport.py::WSTransport`) configured under the `jaato_ws:` config block (`url: ws://...`). When code and these docs disagree, trust the code. `docs/design/ipc-to-ws-migration.md` records the migration.

`WSTransport` still imports event dataclasses from `jaato_sdk.events` (the wire schema), but it does **not** use the SDK's `IPCClient`. Because it is hand-rolled, it must manually send the three wiring events the SDK client would otherwise send on connect — `ClientConfigRequest` (`working_dir` + `config_root`) and a `set_workspace` `CommandRequest` — before `session.new`. The long comment at `session_pool.py:147-173` explains exactly which server-side machinery each of these three wires drives; do not remove any without understanding that.

## Architecture

Telegram update → aiogram router (handler) → `SessionPool` → `WSTransport` → jaato server, then server events stream back through the same transport → `ResponseRenderer` → edited/sent Telegram messages.

**Wiring** — `bot.py::create_bot_and_dispatcher` builds the aiogram `Bot` + `Dispatcher`, constructs every shared singleton (pool, renderer, handlers, optional rate-limiter / abuse-protector / telemetry), and injects them into handlers via `dp["..."]` context keys (aiogram dependency injection — handler params are resolved by name). `__main__.py` owns the run loop, logging, signal handling, polling-vs-webhook startup, and the background idle-cleanup task.

**`session_pool.py` (`SessionPool`)** — one WebSocket connection + one jaato session **per Telegram chat_id**, matching the server's 1-client-1-session model. Key behaviors:
- Dead-transport detection: a cached session is only reused if `transport.connected`; a daemon-dropped WS is recreated inline so the bot self-heals on the next message.
- Session re-attachment: when `session_store_path` is configured, `ChatSessionStore` persists `chat_id → session_id` so a bot restart re-attaches to the same daemon session (verified live via `session.list`) instead of starting a fresh conversation. Unconfigured ⇒ sessions are per-process.
- `forget_session` drops a stuck session (and its persisted mapping) so the next message starts clean — used by the renderer's stall recovery.
- Host-tool assembly + registration happens here (see below).

**`renderer.py` (`ResponseRenderer`)** — the streaming heart. `stream_response` consumes the server event iterator and renders progressively with edit-in-place + throttling. It switches on `jaato_sdk.events.EventType` (AGENT_OUTPUT, AGENT_COMPLETED, TURN_COMPLETED, AGENT_STATUS_CHANGED, INIT_PROGRESS, SYSTEM_MESSAGE, ERROR, PERMISSION_INPUT_MODE, CLARIFICATION_BATCH). It owns Telegram's quirks: HTML escaping, 4096-char splitting at paragraph boundaries, wide-content collapsing into expandable blockquotes, and stall detection (returns `ctx.stalled`). It delegates permission UI to `PermissionHandler`, clarifications to `ClarificationHandler`, and file output to `FileHandler`.

**Handlers (`handlers/`)** — aiogram routers, registered in a specific order in `bot.py` (admin, lifecycle, commands, callbacks, group, then private with whitelist middleware). `private.py` and `group.py` are the message entry points; both follow: rate/abuse checks → `get_or_create_session` → `send_message` → `stream_response` → stall recovery. `private.py` also handles inbound photos/PDFs as base64 user-message attachments for the profile's vision tier. `callbacks.py` handles inline-keyboard taps (permission approvals, expand buttons). `admin.py` is large and holds ban/telemetry/whitelist admin commands.

**Cross-cutting singletons** — `whitelist.py` (username/chat access control + access-request flow, applied as middleware to the private router only), `rate_limiter.py` (token bucket, admin bypass), `abuse_protection.py` (reputation + escalating bans), `telemetry.py` (bot-layer-only metrics — deliberately does *not* duplicate server metrics). All are optional and constructed only when enabled in config.

## Host tools (client-provided tools)

The bot registers tools the *model* can call back into over the same WS (`tools.register_client` → server sends `tool.execute_request` → transport dispatches to a session executor). Four are fixed built-ins in `host_tools.py`: `send_to_telegram`, `show_image`, `register_tool`, `service_manifest`.

`register_tool` is the **self-extension** path: the confined runner writes a draft to `<workspace>/tool_drafts/<name>.py`; after the user approves, the **unconfined bot** copies it into `host_tools_dir`, validates by loading, and re-registers host tools on the live session (`session_pool.py::install_and_register_tool`). `host_tools_dir` **must** be outside the workspace so the AppArmor-confined runner cannot tamper with installed tool code. Dynamic-tool loading is in `host_tool_loader.py`. Reference implementations live in `examples/host_tools/`; deep docs in `docs/features/host-tools.md` and `docs/features/service-checklist.md`.

## Configuration

`config.py` is a Pydantic model tree loaded from YAML with `${ENV_VAR}` substitution. Start from `config.example.yaml`. Notable blocks: `telegram` (token, `mode: polling|webhook`, `access`, `group`), `jaato_ws` (`url`, `tls`, optional Keycloak auth, `profile`/`agent`/`workspace`/`host_tools_dir`), `session` (`max_concurrent`, `session_store_path`), `permissions`, `rendering`, `rate_limiting`, `abuse_protection`, `telemetry`. Logging follows the jaato-sdk standard: set `JAATO_TRACE_LOG` to redirect all logs to a file (console prints only the file path).

## Repo conventions

- **No hardcoded fallbacks.** Empty config string = feature disabled, deliberately (e.g. `host_tools_dir`, `session_store_path`, `workspace`, `profile`). Do not invent default paths to "make it work" — that pattern is intentionally avoided throughout this codebase (see the config docstrings, which call it out explicitly).
- `EventType` and all wire dataclasses come from `jaato_sdk.events` — never re-define them locally.
- The renderer's tight comments flag known drift between this client and server PRs (search for "drift" / PR numbers). Treat those comments as load-bearing.
- `docs/` is organized as `features/`, `design/`, `fixes/`, `implementation/` — check `features/` first for how a user-facing capability is meant to work, `fixes/` for the reasoning behind a recovery/edge-case behavior.
