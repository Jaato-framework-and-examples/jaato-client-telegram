# jaato-client-telegram

A standalone Telegram bot client that bridges Telegram conversations with a [jaato](https://github.com/apanoia/jaato) AI agent server via the jaato-sdk IPC interface.

## Overview

`jaato-client-telegram` is a **client**, not a plugin. It's a separate Python application that:

- Connects to a running jaato server via Unix socket IPC
- Provides a Telegram bot interface for interacting with the AI agent
- Manages isolated sessions for each Telegram user
- Streams agent responses progressively as events arrive

The agent logic, tool execution, plugins, and permissions all remain in the jaato server. This client is simply another I/O surface.

## Features

- **User whitelist** - Username-based access control (see [WHITELIST.md](WHITELIST.md))
- **Multi-user support** - Each Telegram user gets their own isolated agent session
- **Workspace isolation** - Complete filesystem-level segregation with per-user `.env` and `.jaato/` directories
- **Progressive streaming** - Responses stream in real-time as the agent generates them
- **Session management** - Automatic cleanup of idle sessions and workspace directories
- **Text-only messaging** - Send text messages, receive agent responses
- **Long message handling** - Automatically splits long responses at paragraph boundaries
- **Bot commands** - `/start`, `/reset`, `/status`, `/help`, plus admin commands
- **Graceful shutdown** - Properly disconnects all sessions on exit
- **🆃 Expandable content** - Wide tool outputs (JSON, code, tables) automatically collapsed for mobile
- **🆃 Presentation awareness** - Agent adapts output format based on Telegram's constraints
- **🛡️ Rate limiting** - Token bucket algorithm to prevent abuse with per-user limits and admin bypass

## Architecture

```
Telegram Users
       │
       ▼
┌─────────────────────────────┐
│  jaato-client-telegram      │
│  - Session Pool             │
│  - Event Renderer           │
│  - Message Handlers         │
└─────────────────────────────┘
       │ (IPC via jaato-sdk)
       ▼
┌─────────────────────────────┐
│  jaato Server               │
│  - Agent Logic              │
│  - Tools & Plugins          │
│  - Session Management       │
└─────────────────────────────┘
```

## Prerequisites

1. **jaato server** - A running jaato server instance with Unix socket IPC enabled
2. **Python 3.10+** - Required for aiogram v3.x
3. **Telegram Bot Token** - Obtain from [@BotFather](https://t.me/botfather) on Telegram

## Installation

### From Source

```bash
git clone https://github.com/yourusername/jaato-client-telegram.git
cd jaato-client-telegram
pip install -e .
```

### Development Installation

```bash
pip install -e ".[dev]"
```

### Optional Dependencies

```bash
# Multimodal support (Phase 3 - future)
pip install -e ".[multimodal]"

# OpenTelemetry observability (Phase 3 - future)
pip install -e ".[observability]"
```

## Configuration

### Basic Config

1. Copy the example configuration:

```bash
cp config.example.yaml jaato-client-telegram.yaml
```

2. Edit `jaato-client-telegram.yaml`:

```yaml
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"  # Set as environment variable

jaato:
  socket_path: "/tmp/jaato.sock"  # Path to jaato server socket
```

3. Set your bot token as an environment variable:

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token-here"
```

Or hardcode it in the config file (not recommended for production).

### Whitelist Configuration (Optional)

The bot supports username-based whitelisting to restrict access. See [WHITELIST.md](WHITELIST.md) for full documentation.

Quick setup:

```bash
cp whitelist.example.json whitelist.json
# Edit whitelist.json to add your username and admins
```

### Permission UI Configuration (Optional)

The permission request UI can be customized to filter out certain action types that don't work well with Telegram's inline keyboard. See [PERMISSION_APPROVAL_UI.md](PERMISSION_APPROVAL_UI.md) for full documentation.

**Example - Customizing unsupported actions:**

```yaml
# jaato-client-telegram.yaml
permissions:
  # Action types to filter from inline keyboard
  # Supports comma or pipe separation: "comment,edit,idle" or "comment|edit|idle"
  unsupported_actions: "comment,edit,modify,custom,input"
```

Or via environment variable:

```bash
export JAATO_PERMISSION_UNSUPPORTED_ACTIONS="comment|edit|idle|turn|all"
```

### Trace Log Configuration (jaato-sdk Client Standard)

This client follows the jaato-sdk client standard for trace logging:

**Standard Behavior:**

- **If `JAATO_TRACE_LOG` environment variable is set and non-empty**: All logs are written to the specified file. Only a single console message indicates where logs are being written.

  ```bash
  export JAATO_TRACE_LOG="/var/log/jaato-client-telegram.log"
  ```

  Console output:
  ```
  📝 Logs are being written to: /var/log/jaato-client-telegram.log
  ```

- **If `JAATO_TRACE_LOG` is not set**: Logs are sent to the console (stderr) as usual.

This standard ensures consistent logging behavior across all jaato-sdk based clients.

### Abuse Protection (Optional)

The bot includes an abuse protection system that detects and mitigates abusive behavior:

**Features:**

- **Suspicious Activity Detection** - Detects rapid messaging and spam patterns
- **User Reputation System** - Tracks user trust scores (0-100)
- **Automatic Escalation** - Warnings → Temporary bans → Permanent bans
- **Admin Management** - Ban/unban users, view abuse statistics

**Configuration:**

```yaml
# jaato-client-telegram.yaml
abuse_protection:
  enabled: true
  max_rapid_messages: 5          # Max messages in rapid interval
  rapid_message_interval: 3      # Interval in seconds
  suspicion_threshold: 70        # Score threshold for escalation
  reputation_threshold: 30.0     # Below this: bans apply
  temporary_ban_duration: 300    # Seconds
  admin_bypass: true
```

**Admin Commands:**

```bash
/ban <user_id> [reason]           # Ban a user (permanent by default)
/ban <user_id> --temp [reason]     # Temporary ban
/unban <user_id>                   # Unban a user
/abuse_stats                       # View abuse statistics
```

### Telemetry (Optional)

The bot includes minimal bot-layer telemetry that collects metrics not tracked by jaato-server:

**What's Collected:**

- **Telegram Delivery** - Message success/failure rates, API errors
- **UI Interactions** - Permission approvals, command usage, button clicks, message edits
- **Session Pool** - Active connections, utilization, errors
- **Rate Limiting** - Users limited, cooldowns triggered
- **Abuse Protection** - Bans applied, warnings issued
- **Latency** - End-to-end latency (avg, P50, P95, P99)

**Configuration:**

```yaml
# jaato-client-telegram.yaml
telemetry:
  enabled: false
  collect_telegram_delivery: true
  collect_ui_interactions: true
  collect_session_pool: true
  collect_rate_limiting: true
  collect_abuse_protection: true
  collect_latency: true
  retention_hours: 24
  cleanup_interval_minutes: 60
```

**Admin Commands:**

```bash
/telemetry    # View all telemetry statistics
```

**Example Output:**
```
📊 Telemetry Statistics

Uptime: 2.3 hours

📤 Telegram API:
  Sent: 142
  Failed: 3
  Error rate: 2.1%
  Errors (1h): 2

🖱️ UI Interactions:
  Permissions: 15✅ / 3❌
  Message edits: 47
  Collapsible expands: 8
  Top commands: /start, /reset, /help

🔗 Session Pool:
  Active: 5/50
  Utilization: 10.0%
  Errors: 0
  Avg session: 45.2s

⏱️ Rate Limiting:
  Users limited: 2
  Cooldowns: 5
  Active limited: 1

🛡️ Abuse Protection:
  Bans applied: 3
  Temporary: 2
  Permanent: 1
  Warnings: 7

⚡ Latency (end-to-end):
  Avg: 2345ms
  P50: 1980ms
  P95: 4120ms
  P99: 5780ms
  Requests: 142
```

**Note:** This telemetry is minimal and bot-layer only. It does NOT duplicate metrics already collected by jaato-server (agent execution, tool usage, token counts, etc.).

## Running

### Start jaato Server

First, ensure your jaato server is running with IPC enabled:

```bash
# In your jaato repository
python -m jaato
```

The server should create a Unix socket at `/tmp/jaato.sock` (or your configured path).

### Start the Telegram Bot

```bash
# Using the installed script
jaato-tg

# Or via Python module
python -m jaato_client_telegram

# With custom config path
jaato-tg --config /path/to/config.yaml
```

The bot will:
1. Load configuration
2. Connect to jaato server via IPC
3. Start polling for Telegram messages (default mode)
4. Run background cleanup of idle sessions

#### Polling Mode (Default)

Polling mode is simpler for development and testing. The bot polls Telegram's API for updates periodically.

```yaml
# jaato-client-telegram.yaml
telegram:
  mode: "polling"  # Default mode
```

#### Webhook Mode (Production)

Webhook mode is recommended for production deployments. Telegram sends updates to your server in real-time, reducing latency and server load.

**Configuration:**

```yaml
# jaato-client-telegram.yaml
telegram:
  mode: "webhook"
  webhook:
    url: "https://your-domain.com/tg-webhook"
    host: "0.0.0.0"
    port: 8443
    path: "/tg-webhook"
    secret_token: "${WEBHOOK_SECRET}"
```

**Generate a webhook secret:**

```bash
openssl rand -hex 32
# Set this as WEBHOOK_SECRET environment variable
```

**Deployment Requirements:**

1. **Public HTTPS URL**: Telegram only sends webhooks to HTTPS endpoints
2. **Reverse Proxy**: Use nginx, Caddy, or Apache to proxy requests to the webhook server
3. **SSL Certificate**: Valid SSL certificate (Let's Encrypt recommended)

**Example nginx configuration:**

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location /tg-webhook {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Example Caddy configuration:**

```caddy
your-domain.com {
    reverse_proxy 127.0.0.1:8443
}
```

**Systemd service example:**

```ini
[Unit]
Description=jaato-client-telegram
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/path/to/jaato-client-telegram
Environment="TELEGRAM_BOT_TOKEN=your-bot-token"
Environment="WEBHOOK_SECRET=your-webhook-secret"
ExecStart=/usr/local/bin/jaato-tg --config /path/to/jaato-client-telegram.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Switching between modes:**

```bash
# Polling mode (development)
jaato-tg --config jaato-client-telegram-polling.yaml

# Webhook mode (production)
jaato-tg --config jaato-client-telegram-webhook.yaml
```

## Usage

### Private Chats

1. **Start a conversation** - Send `/start` to the bot in a private DM
2. **Send messages** - Just type and send text messages
3. **Reset session** - Use `/reset` to clear your conversation state
4. **Check status** - Use `/status` to see active sessions and your connection state
5. **Get help** - Use `/help` to see available commands

### Group Chats

The bot also works in Telegram groups and supergroups!

**How to trigger the bot:**

- **Mention the bot** - Type `@your_bot_username` followed by your message
- **Use trigger prefix** - Configure a prefix like `!ask` to trigger without mentioning
- **Reply to bot** - Replying to a bot message will also trigger a response

**Configuration:**

```yaml
# jaato-client-telegram.yaml
telegram:
  group:
    require_mention: true  # Only respond when mentioned or reply
    trigger_prefix: "!ask"   # Optional: use prefix instead of mention
```

**Session Isolation in Groups:**

Each user in a group gets their own isolated session, even when chatting in the same group:
- User A's conversation with the bot is private to User A
- User B's conversation with the bot is private to User B
- Sessions are not shared between users

**Group Commands:**

- `/help` - Show group usage instructions
- `/reset` - Reset your personal session

**Example Group Interaction:**

```
User A: @mybot What's the weather today?
Bot A: [Responds with weather info - visible to all]

User B: /reset
Bot: 🔄 Session reset. [Only User B sees this]
```

## Project Structure

```
jaato-client-telegram/
├── pyproject.toml              # Package dependencies
├── README.md                   # This file
├── config.example.yaml         # Example configuration
├── .env                        # Root environment template
├── .jaato/                     # Root jaato directory template
└── src/jaato_client_telegram/
    ├── __init__.py
    ├── __main__.py             # Entry point
    ├── config.py               # Configuration model
    ├── bot.py                  # Bot & dispatcher setup
    ├── session_pool.py         # Per-user SDK client management
    ├── workspace.py            # Per-user workspace isolation
    ├── renderer.py             # Response streaming & formatting
    └── handlers/
        ├── __init__.py
        ├── admin.py            # Admin commands
        ├── callbacks.py        # Callback query handlers
        ├── commands.py        # /start, /reset, /status, /help
        ├── group.py           # Group chat message handler
        └── private.py         # Private chat message handler
```

## Workspace Isolation

Each Telegram user gets their own isolated workspace:

```
workspaces/
├── user_123456789/             # Per-user workspace
│   ├── .env                    # User's environment variables
│   └── .jaato/                 # User's memories, waypoints, templates
│       ├── memory/
│       ├── waypoints/
│       └── templates/
└── user_987654321/
    ├── .env
    └── .jaato/
```

**Benefits:**
- Complete isolation of user data and configurations
- Each user has their own `.env` for custom environment variables
- Each user has their own `.jaato/` for memories, waypoints, and templates
- Automatic cleanup when sessions expire or `/reset` is used

## Development

### Running Tests

```bash
pytest tests/
```

### Code Formatting

```bash
black src/
ruff check src/
mypy src/
```

## Roadmap

### Phase 1: MVP (Current)
- ✅ Text-only messaging
- ✅ Session pool with per-user isolation
- ✅ Progressive response streaming
- ✅ Basic commands (/start, /reset, /status, /help)
- ✅ Long message splitting
- ✅ Polling mode
- ✅ Idle session cleanup

### Phase 2: Interactive Features (Planned)
- Permission approval via inline keyboards
- Webhook mode support
- Group chat support with mention filtering
- Enhanced streaming with edit throttling
- `/help` command with examples

### Phase 3: Advanced (Planned)
- Multimodal support (images, files, voice)
- OpenTelemetry observability
- Rate limiting per user
- Abuse protection
- Voice message transcription

## Troubleshooting

### "Failed to connect to jaato server"

- Ensure jaato server is running: `ps aux | grep jaato`
- Check socket path matches in both jaato and client config
- Verify socket exists: `ls -l /tmp/jaato.sock`

### Bot doesn't respond to messages

- Check bot token is correct
- Verify bot is running and connected to jaato
- Check logs for errors
- Try `/reset` to clear your session

### "Permission denied" errors

- Ensure jaato server socket has proper permissions
- Check that your user can read/write the socket file

## Contributing

Contributions welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Links

- [jaato Server](https://github.com/Jaato-framework-and-examples/jaato)
- [jaato Documentation](https://jaato-framework-and-examples.github.io/jaato/)
- [jaato-sdk Reference](https://github.com/Jaato-framework-and-examples/jaato-sdk)
- [aiogram Documentation](https://docs.aiogram.dev/)

## Acknowledgments

Built with:
- [jaato-sdk](https://github.com/Jaato-framework-and-examples/jaato-sdk) - IPC client for jaato
- [aiogram](https://github.com/aiogram/aiogram) - Async Telegram bot framework
- [Pydantic](https://github.com/pydantic/pydantic) - Configuration validation
