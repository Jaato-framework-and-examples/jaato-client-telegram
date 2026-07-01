# jaato-client-telegram

A standalone Telegram bot that bridges Telegram chats to a running [jaato](https://github.com/Jaato-framework-and-examples/jaato) AI agent server over a **WebSocket** connection.

## Overview

`jaato-client-telegram` is a **client**, not a plugin. It's a separate Python application that:

- Connects to a running jaato server over a WebSocket (the `jaato_ws:` config block)
- Provides a Telegram bot interface for talking to the AI agent
- Manages one isolated agent session per Telegram chat
- Streams agent responses progressively as events arrive, editing messages in place

All agent logic, tool execution, plugins, and permissions live in the **jaato server**. This process is purely an I/O surface: it translates Telegram messages into jaato session events and renders the streamed event responses back into Telegram messages.

> **Transport note:** earlier revisions of this project spoke to the server over a Unix-socket "jaato-sdk IPC" connection (`/tmp/jaato.sock`, `socket_path`). That is no longer the case — the live transport is a WebSocket configured under `jaato_ws:` (`url: ws://…` / `wss://…`). The client imports the wire schema (event dataclasses) from `jaato_sdk.events`.

## Deploy to a VPS (one command)

`deploy-vps.sh` bootstraps the **whole stack** — the jaato server *and* this bot — onto a fresh Linux VPS: it installs system deps, clones + installs the jaato monorepo (server + SDK) and this bot into a venv, asks for your provider/model/keys, generates the config + a **user whitelist**, wires two **systemd** services (server + bot), and runs a layered health check. Premium-free, no TLS or inbound ports (bot ↔ server over `ws://localhost`, Telegram polling).

On a fresh **Ubuntu/Debian** VPS (as root, or a sudo user):

```bash
curl -fsSL https://raw.githubusercontent.com/Jaato-framework-and-examples/jaato-client-telegram/master/deploy-vps.sh -o deploy-vps.sh
chmod +x deploy-vps.sh
./deploy-vps.sh          # prompts for token, provider, model, key, whitelist
```

It prompts for:
- your **Telegram bot token** ([@BotFather](https://t.me/botfather))
- the **provider + model + API key** — the provider menu and each provider's key env-var are discovered from `jaato-scaffold explain` (no hardcoded list: ZhipuAI, OpenRouter, Anthropic, OpenAI, Gemini, Groq, …), with an optional **separate vision tier** (different provider/model/key)
- the **whitelist** — admin + allowed Telegram usernames

**Non-interactive** (automation/CI) — provide the same values as env vars:

```bash
TELEGRAM_BOT_TOKEN=… \
EXEC_PROVIDER=zhipuai EXEC_MODEL=glm-4.6 EXEC_KEY=… \
VISION_PROVIDER=openrouter VISION_MODEL=google/gemini-2.5-flash VISION_KEY=… \
WHITELIST_ADMINS=yourhandle WHITELIST_USERS=friend1,friend2 \
./deploy-vps.sh
```

Idempotent (re-run to upgrade); `./deploy-vps.sh --uninstall` to remove the services. Secrets go to `chmod 600` env files — never inlined in the profile. As root it installs **system** units (`systemctl status jaato-server jaato-tg`); as a non-root user, **`--user`** units with linger. Validated on a fresh Ubuntu 26.04 / Python 3.14 VPS. Design notes: [docs/design/vps-bootstrap-feasibility.md](docs/design/vps-bootstrap-feasibility.md).

> Prefer to run the server and bot yourself, or connect to an existing jaato server? See **Configuration** and **Running** below.

## Features

- **WebSocket transport** — one connection + one jaato session per Telegram `chat_id`, matching the server's 1-client-1-session model; optional TLS (`wss://`) and Keycloak auth.
- **Session re-attachment** — with `session.session_store_path` set, a bot restart re-attaches to the same daemon session (verified via `session.list`) instead of starting a fresh conversation.
- **Progressive streaming** — responses stream in real time with edit-in-place and throttling; long replies split at paragraph boundaries (Telegram's 4096-char limit).
- **Multi-user isolation** — every chat gets its own session; conversations are not shared.
- **Group chat support** — mention / reply / trigger-prefix activation, per-user sessions within a group.
- **Permission approvals** — gated tools surface as inline-keyboard prompts with the tool's parameters shown for review (oversized payloads delivered as files).
- **Clarification requests** — the agent can ask the user structured questions and receive batched answers.
- **Vision attachments** — inbound photos and PDFs are forwarded as base64 user-message attachments for the profile's vision tier.
- **Expandable content** — wide tool outputs (JSON, code, tables) are collapsed behind a "show more" affordance for mobile.
- **Presentation awareness** — the bot tells the server its display capabilities (narrow width, no wide tables, images + expandable supported) so the model adapts its output.
- **Self-extending host tools** — the agent builds new tools on request via `register_tool`; four core tools (`send_to_telegram`, `show_image`, `register_tool`, `service_manifest`) ship as fixed built-ins. See [docs/features/host-tools.md](docs/features/host-tools.md) and the runnable [examples/host_tools/](examples/host_tools/).
- **Session-startup services** — a per-agent manifest of host tools is checked/started at the start of every session via a deterministic prefetch checklist. See [docs/features/service-checklist.md](docs/features/service-checklist.md).
- **Thread continuity** — bot replies and host-tool messages follow the Telegram thread the user is writing in.
- **Rate limiting** — token-bucket per-user limits with admin bypass.
- **Abuse protection** — reputation tracking with escalating warnings/bans.
- **Telemetry** — minimal bot-layer metrics (does not duplicate server metrics).
- **User whitelist** — username/chat access control with an access-request flow (see [WHITELIST.md](WHITELIST.md)).
- **Graceful shutdown** — detaches all sessions on exit.

## Architecture

```
Telegram update
       │
       ▼
aiogram router (handler)
       │
       ▼
┌─────────────────────────────┐
│  jaato-client-telegram      │
│  - SessionPool              │   one WS connection + one session per chat_id
│  - ResponseRenderer         │   streams events → edited Telegram messages
│  - Handlers / Permissions   │
└─────────────────────────────┘
       │  WebSocket  (jaato_ws: url)
       ▼
┌─────────────────────────────┐
│  jaato Server               │
│  - Agent logic              │
│  - Tools & plugins          │
│  - Permissions              │
│  - Session management        │
└─────────────────────────────┘
```

Server events stream back over the same WebSocket and are rendered progressively.

## Prerequisites

1. **jaato server** — a running jaato server reachable over WebSocket (`ws://` or `wss://`).
2. **Python 3.10+** — required for aiogram v3.x.
3. **Telegram Bot Token** — from [@BotFather](https://t.me/botfather).

## Installation

```bash
git clone https://github.com/Jaato-framework-and-examples/jaato-client-telegram.git
cd jaato-client-telegram
pip install -e .            # or: pip install -e ".[dev]" for tooling
```

## Configuration

Configuration is a YAML file (`jaato-client-telegram.yaml`) with `${ENV_VAR}` substitution. Start from the example:

```bash
cp config.example.yaml jaato-client-telegram.yaml
```

Top-level blocks: `telegram`, `jaato_ws`, `session`, `rendering`, `permissions`, `file_sharing`, `rate_limiting`, `abuse_protection`, `telemetry`, `logging`. The key one is the transport:

```yaml
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"   # never inline secrets; use ${ENV} / pass://
  mode: "polling"                       # or "webhook"

jaato_ws:
  url: "wss://localhost:8089"           # ws:// for plain (dev only), wss:// for TLS
  tls:
    enabled: true
  secret_token: "${JAATO_WS_TOKEN}"     # optional shared-secret auth
  profile: ""                            # server-side profile name (empty = server default)
  agent: ""                              # server-side agent persona (empty = none)
  workspace: "${JAATO_TG_WORKSPACE}"     # server-side workspace path (.jaato/ lives here)
  host_tools_dir: "${JAATO_TG_HOST_TOOLS_DIR}"   # bot-owned install dir, OUTSIDE the workspace

session:
  max_concurrent: 50
  session_store_path: "${JAATO_TG_SESSION_STORE}"  # enables re-attach across restarts
```

> **No hardcoded fallbacks.** An empty config string means a feature is deliberately disabled (e.g. `host_tools_dir`, `session_store_path`, `workspace`, `profile`). The bot never invents default paths. `host_tools_dir` **must** be outside the workspace so the server's confined runner cannot tamper with installed tool code.

### Whitelist (optional)

Username/chat access control with an access-request flow. See [WHITELIST.md](WHITELIST.md).

```bash
cp whitelist.example.json whitelist.json   # edit to add your username + admins
```

### Permission UI (optional)

The inline-keyboard permission prompt can be tuned — which action buttons are primary, which to filter out, and which tools render their argument as code. See [docs/features/permission-approval-ui.md](docs/features/permission-approval-ui.md).

```yaml
permissions:
  primary_actions: "yes,no,always,never"        # buttons shown prominently
  unsupported_actions: "comment,edit,modify"    # filtered from the keyboard
  code_extensions: "notebook_execute:py"        # render this tool's arg as a .py file
```

### Trace logging (jaato-sdk client standard)

Set `JAATO_TRACE_LOG` to redirect all logs to a file (the console then prints only the file path); unset, logs go to stderr.

```bash
export JAATO_TRACE_LOG="/var/log/jaato-client-telegram.log"
```

### Rate limiting / Abuse protection / Telemetry (optional)

All three are off by default and constructed only when enabled.

```yaml
rate_limiting:
  enabled: true

abuse_protection:
  enabled: true
  max_rapid_messages: 5
  rapid_message_interval: 3
  temporary_ban_duration: 300
  admin_bypass: true

telemetry:
  enabled: false          # bot-layer metrics only; does NOT duplicate server metrics
```

Admin commands: `/ban <user_id> [--temp] [reason]`, `/unban <user_id>`, `/abuse_stats`, `/telemetry`.

## Running

Start the jaato server (separate project) so it is listening on your configured WebSocket URL, then:

```bash
jaato-tg                                   # installed script
python -m jaato_client_telegram            # or via module
jaato-tg --config <path> --whitelist <path>
./start.sh                                 # guided first-run: scaffolds config, checks token + server port
```

The bot loads config, connects to the server over WebSocket, starts polling (default) or the webhook server, and runs a background idle-session cleanup task.

### Polling mode (default)

```yaml
telegram:
  mode: "polling"
```

### Webhook mode (production)

```yaml
telegram:
  mode: "webhook"
  webhook:
    url: "https://your-domain.com/tg-webhook"
    host: "0.0.0.0"
    port: 8443
    path: "/tg-webhook"
    secret_token: "${WEBHOOK_SECRET}"
```

Webhook deployment needs a public HTTPS URL (Telegram only delivers to HTTPS), a reverse proxy (nginx/Caddy), and a valid certificate.

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;
    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;
    location /tg-webhook {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

```ini
# /etc/systemd/system/jaato-client-telegram.service
[Unit]
Description=jaato-client-telegram
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/jaato-client-telegram
ExecStart=/usr/local/bin/jaato-tg --config /path/to/jaato-client-telegram.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Usage

### Private chats

- `/start` — begin a conversation
- send text, photos, or PDFs — the agent responds (photos/PDFs go to the vision tier)
- `/reset` — drop your session and start fresh
- `/status` — active sessions and your connection state
- `/help` — available commands

### Group chats

Activate the bot by **mentioning** it (`@your_bot`), **replying** to one of its messages, or using a configured **trigger prefix**. Each user gets their own isolated session within the group.

```yaml
telegram:
  group:
    require_mention: true
    trigger_prefix: "!ask"
```

## Host tools & self-extension

The bot registers tools the *model* can call back into over the same WebSocket. Four are fixed built-ins (`send_to_telegram`, `show_image`, `register_tool`, `service_manifest`). The rest are **self-extension**: the agent writes a draft to `tool_drafts/<name>.py`, and after you approve, the (unconfined) bot installs it into `host_tools_dir` and re-registers it on the live session. See [docs/features/host-tools.md](docs/features/host-tools.md).

### Shipped example tools

Real tools the bot built dynamically during use, promoted to [examples/host_tools/](examples/host_tools/) as references. Secrets and personal data were scrubbed; credential files are never shipped (e.g. `gmail` expects you to supply your own OAuth credentials at runtime via its `configure` action). Some are region-specific.

| Tool | What it does | Notes | Demo |
|------|--------------|-------|------|
| `weather` | Today/tomorrow forecast via Open-Meteo | no API key | [📷](examples/host_tools/screenshots/weather.jpg) |
| `youtube_search` | Top YouTube results (title, URL, duration, channel) | no key | |
| `image_search` | Find and show a web image inline | | |
| `moon_phase` | Today's moon phase as a PIL-rendered image + illumination % | image output | [📷](examples/host_tools/screenshots/moon_phase.jpg) |
| `daily_ephemerides` | Astronomy ephemerides on demand or scheduled daily | scheduling | |
| `screenshot` | Render a Telegram-style chat screenshot from messages | image output | |
| `ttt` | Tic-tac-toe with an inline-button board + board image | interactive | |
| `die_roller` | Roll dice (`d20`, `2d6`, `1d20+3`) | | |
| `reverse_text` | Reverse a string | minimal demo | |
| `docker_status` | Docker container status (running/stopped/total) | host shell-out | [📷](examples/host_tools/screenshots/docker_status.jpg) |
| `docker_report` | Filtered Docker container report | host shell-out | |
| `remind` | Create/list/cancel reminders delivered as Telegram messages | scheduling | |
| `shopping_list` | Persistent shopping list (add/list/remove) | workspace state | |
| `gmail` | Gmail over OAuth2/Gmail API (configure/auth/check/read/watch/doctor) | creds user-supplied | [📷](examples/host_tools/screenshots/gmail.jpg) |
| `approval_webhook` | In-process webhook server that asks the user to approve/deny via buttons | server + `ask_user` | |
| `mercadona` | Mercadona grocery via an external CLI (search/cart/postal) | region: Spain | |
| `spanish_news` | RSS digest from public Spanish news sources | region: Spain | [📷](examples/host_tools/screenshots/spanish_news.jpg) |

## Project structure

```
jaato-client-telegram/
├── pyproject.toml
├── config.example.yaml          # example configuration
├── jaato-client-telegram.yaml   # tracked live config (paths only; secrets are ${ENV})
├── start.sh                     # guided first-run
├── docs/                        # features/ design/ fixes/ implementation/
├── examples/                    # reference host tools + example service manifest
├── runtime/                     # bot workspace (gitignored; tracked: .jaato/ profile + agent)
├── tests/
└── src/jaato_client_telegram/
    ├── __main__.py              # run loop, logging, signals, polling/webhook startup, idle cleanup
    ├── bot.py                   # builds Bot + Dispatcher, wires singletons into handlers
    ├── config.py                # Pydantic config model tree
    ├── transport.py             # WebSocket transport to the jaato server
    ├── session_pool.py          # one WS connection + one session per chat_id; re-attach
    ├── renderer.py              # event stream → edited/sent Telegram messages
    ├── permissions.py           # inline-keyboard permission UI
    ├── clarification.py         # structured clarification questions
    ├── file_handler.py          # file delivery
    ├── host_tools.py            # built-in host tools
    ├── host_tool_loader.py      # loader for agent-installed dynamic tools
    ├── chat_session_store.py    # chat_id → session_id persistence (re-attach)
    ├── thread_store.py          # per-chat Telegram thread continuity
    ├── thread_bot.py            # injects the current thread into host-tool sends
    ├── whitelist.py             # access control + access-request flow
    ├── rate_limiter.py          # token-bucket limiter
    ├── abuse_protection.py      # reputation + escalating bans
    ├── telemetry.py             # bot-layer metrics
    ├── semantic_markup.py       # markdown → Telegram HTML helpers
    └── handlers/
        ├── admin.py             # ban/telemetry/whitelist admin commands
        ├── callbacks.py         # inline-keyboard taps (permissions, expand)
        ├── commands.py          # /start, /reset, /status, /help
        ├── filters.py           # aiogram filters
        ├── group.py             # group chat handler
        ├── lifecycle.py         # startup/shutdown hooks
        └── private.py           # private chat handler (text + photo/PDF)
```

## Session & workspace model

The bot uses **one WebSocket connection and one jaato session per Telegram chat**. There is a single server-side `workspace` (the `jaato_ws.workspace` directory); the server runs each chat's session inside it. Key behaviors:

- **Re-attach** — when `session_store_path` is set, `chat_id → session_id` is persisted so a restart re-attaches to the same daemon session (history preserved) instead of starting over. Unset ⇒ sessions are per-process.
- **Self-healing** — a cached session is reused only if its WebSocket is alive; a dropped connection is recreated on the next message.
- **Idle detach** — idle chats are detached so the server can free runners for active chats; the first message after a detach pays a cold-revive cost (re-spawn + restore under the same id). This trade is intentional — see [docs/design/session-lifecycle.md](docs/design/session-lifecycle.md).

## Development

```bash
pip install -e ".[dev]"
pytest tests/                 # unit tests run fully offline (transport/server mocked)
black src/                    # format (line-length 100)
ruff check src/               # lint (E, F, I, N, W)
mypy src/                     # type-check
```

Integration testing is manual and needs a live jaato server + a real bot token — see [TESTING.md](TESTING.md).

## Troubleshooting

**"Failed to connect to jaato server"**
- Ensure the jaato server is running and listening on your configured WebSocket URL.
- Check that `jaato_ws.url` matches the server's bind address/port.
- For `wss://`, verify the TLS settings (`jaato_ws.tls`) and certificate.

**Bot doesn't respond**
- Verify the bot token is correct and the bot is running.
- Check the logs (`JAATO_TRACE_LOG`) for errors.
- Try `/reset` to drop a stuck session.

**Auth failures**
- If the server requires it, set `jaato_ws.secret_token` (or the Keycloak fields). Empty ⇒ the connection is anonymous.

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT License — see [LICENSE](LICENSE).

## Links

- [jaato Server](https://github.com/Jaato-framework-and-examples/jaato)
- [jaato Documentation](https://jaato-framework-and-examples.github.io/jaato/)
- [jaato-sdk](https://github.com/Jaato-framework-and-examples/jaato-sdk)
- [aiogram Documentation](https://docs.aiogram.dev/)

## Acknowledgments

Built with:
- [jaato-sdk](https://github.com/Jaato-framework-and-examples/jaato-sdk) — the event/wire schema (`jaato_sdk.events`) this client speaks over WebSocket
- [aiogram](https://github.com/aiogram/aiogram) — async Telegram bot framework
- [Pydantic](https://github.com/pydantic/pydantic) — configuration validation
