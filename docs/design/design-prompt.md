# Design Prompt: jaato-client-telegram

## Objective

Design and implement `jaato-client-telegram`, a standalone application that bridges Telegram conversations with a running jaato server. This is a **client**, not a plugin. It is a separate project with its own repository, dependencies, configuration, and entry point. It connects to jaato through the `jaato-sdk` package — the same SDK used by other jaato clients like the Enphase energy advisor.

The agent logic, tool execution, plugin system, permissions, and observability all remain in the jaato server. This client is simply another I/O surface.

---

## jaato-sdk Interface

The Telegram client's **only** interface to jaato is through `jaato-sdk`. It MUST NOT import from `jaato` core, `jaato.orchestrator`, or any server-side module.

### SDK API Surface

```python
from jaato_sdk.client import IPCRecoveryClient, ConnectionState
from jaato_sdk.events import Event, EventType
```

### IPCRecoveryClient

Constructor:
```python
client = IPCRecoveryClient(
    socket_path="/tmp/jaato.sock",   # Unix socket to jaato server
    auto_start=True,                  # Auto-start server if not running
    env_file=".env"                   # Environment file for server config
)
```

Methods:
```python
await client.connect()               # Connect to jaato server
await client.disconnect()            # Disconnect from jaato server
await client.send_message(text)      # Send a user message
async for event in client.events():  # Stream response events
    ...
```

Properties:
```python
client.state -> ConnectionState      # CONNECTED, DISCONNECTED, etc.
```

### Event Model

Each event has:
```python
event.type -> EventType              # e.g., EventType.AGENT_OUTPUT
event.content -> str                 # The event payload
```

### Communication Pattern

The SDK uses a **message-in, event-stream-out** pattern:

1. Client calls `send_message(text)` — fire and forget
2. Client iterates `events()` — receives a stream of `Event` objects
3. Events arrive incrementally (streaming is native, not bolted on)
4. The stream ends when the agent completes its turn

This is NOT request/response. There is no `process_turn()` that returns a string. The client must accumulate events and render them progressively.

### What the SDK Handles

- IPC transport (Unix socket)
- Connection recovery (automatic reconnection)
- Server lifecycle (auto-start if configured)
- Event deserialization
- Session management (server-side, transparent to clients)

### What the SDK Does NOT Handle

- Session IDs — the server manages sessions internally
- Multi-user routing — the server sees one connection per client
- Message formatting — events contain raw content
- Permission UI — events may signal permission requests, but rendering is client's job
---

## Architectural Principles

### 1. Standalone Application

This is its own Python package: `jaato-client-telegram`. It has its own `pyproject.toml`, its own config file, its own `__main__.py`. Its only interface to jaato is through the `jaato-sdk` pip package.

jaato server does not know this client exists. jaato server does not import it. jaato server has zero Telegram dependencies.

### 2. SDK-Only Boundary

The client MUST only import from `jaato_sdk`. Never from `jaato`, `jaato.orchestrator`, `jaato.plugins`, or any server-internal module. The SDK is the contract. If the client needs something the SDK doesn't expose, the SDK must be extended — the client must not reach around it.

### 3. Clean Separation of Concerns

The client MUST NOT contain any agent logic, LLM calls, or tool execution. Its responsibilities are strictly:

- **Inbound**: Receive Telegram messages, forward to jaato via SDK
- **Outbound**: Receive jaato events via SDK, render as Telegram messages
- **User routing**: Map Telegram users to jaato client instances
- **Lifecycle**: Start, stop, health check, graceful shutdown

### 4. No Channel Awareness in the Agent

The agent never sees Telegram-specific data. Messages arrive as plain text via `send_message()`. Events come back as content strings. The same jaato server works identically whether driven from CLI, the Enphase advisor, this Telegram client, or any future client.

### 5. One SDK Client Per User Session

Since the jaato SDK client represents a single connection to the server, and the server manages session state per connection, the Telegram client needs **one `IPCRecoveryClient` instance per active Telegram user**. This is the key architectural difference from a simple wrapper — the client must manage a pool of SDK connections.
---

## Project Structure

```
jaato-client-telegram/
  pyproject.toml
  README.md
  config.example.yaml
  src/
    jaato_client_telegram/
      __init__.py
      __main__.py              # Entry point: python -m jaato_client_telegram
      config.py                # Pydantic config model, loads client's own YAML
      bot.py                   # aiogram Bot + Dispatcher setup, polling/webhook
      session_pool.py          # Maps chat_id -> IPCRecoveryClient instances
      renderer.py              # Accumulates events, formats for Telegram
      permissions_ui.py        # Inline keyboards for permission approval events
      handlers/
        __init__.py
        private.py             # Private chat message handlers
        group.py               # Group chat handlers (mention filtering)
        commands.py            # /start, /reset, /status, /help
        callbacks.py           # Inline keyboard callback query handlers
  tests/
    ...
```

Note the absence of `bridge.py` — that abstraction is unnecessary. The `jaato-sdk` IS the bridge. The session pool manages SDK client instances directly.
---

## Configuration

The client has its own config file, completely independent from jaato's configuration:

```yaml
# jaato-client-telegram.yaml

# --- Telegram-side configuration ---
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"    # From @BotFather
  mode: "polling"                        # "polling" or "webhook"
  webhook:
    url: "https://your-domain.com/tg-webhook"
    port: 8443
    cert_path: "/path/to/cert.pem"
  access:
    allowed_chat_ids: []                 # Empty = allow all, or whitelist
    admin_user_ids: []                   # Users who can /reset, /status
  group:
    require_mention: true
    trigger_prefix: null                 # Optional: "!ask", "/q"

# --- jaato SDK connection ---
jaato:
  socket_path: "/tmp/jaato.sock"         # Unix socket to jaato server
  auto_start: false                      # Don't auto-start server from client
  env_file: ".env"                       # Passed to IPCRecoveryClient

# --- Session pool ---
session:
  max_concurrent: 50                     # Max simultaneous SDK connections
  idle_timeout_minutes: 60               # Disconnect idle SDK clients
  reconnect_on_error: true               # Use SDK's built-in recovery

# --- Response rendering ---
rendering:
  max_message_length: 4096               # Telegram's limit
  stream_edits: true                     # Edit-in-place as events arrive
  typing_indicator: true                 # Send "typing..." while waiting
  edit_throttle_ms: 500                  # Min interval between edit_message_text calls

# --- Logging ---
logging:
  level: "INFO"
  format: "structured"
```
---

## Core Architecture

### Message Flow

```
Telegram User A ─┐
                  │
Telegram User B ─┤
                  │
Telegram User C ─┤
                  ▼
         ┌─────────────────────────┐
         │  jaato-client-telegram   │
         │                          │
         │  ┌────────────────────┐  │
         │  │ aiogram Bot        │  │  ◄── polling or webhook
         │  │ + Dispatcher       │  │
         │  └────────┬───────────┘  │
         │           │              │
         │  ┌────────▼───────────┐  │
         │  │ Session Pool       │  │  ◄── chat_id -> IPCRecoveryClient
         │  │                    │  │
         │  │  User A ──► client_a  │
         │  │  User B ──► client_b  │
         │  │  User C ──► client_c  │
         │  └────────┬───────────┘  │
         │           │              │
         │  ┌────────▼───────────┐  │
         │  │ Renderer           │  │  ◄── events -> Telegram messages
         │  └────────────────────┘  │
         └─────────────────────────┘
                     │
                     │  Unix socket (one per user)
                     ▼
         ┌─────────────────────────┐
         │  jaato server            │
         │  (separate process)      │
         │  - orchestrator          │
         │  - plugins, tools        │
         │  - sessions, permissions │
         │  - OpenTelemetry         │
         └─────────────────────────┘
```

### Session Pool

The critical component. Each Telegram user gets their own `IPCRecoveryClient` instance:

```python
class SessionPool:
    """Manages a pool of jaato SDK client connections, one per Telegram user."""

    def __init__(self, config: JaatoConfig, max_concurrent: int = 50):
        self._config = config
        self._max_concurrent = max_concurrent
        self._clients: dict[int, IPCRecoveryClient] = {}  # chat_id -> client
        self._last_activity: dict[int, datetime] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, chat_id: int) -> IPCRecoveryClient:
        """Get or create an SDK client for this chat_id."""
        async with self._lock:
            if chat_id not in self._clients:
                if len(self._clients) >= self._max_concurrent:
                    await self._evict_oldest()

                client = IPCRecoveryClient(
                    socket_path=self._config.socket_path,
                    auto_start=self._config.auto_start,
                    env_file=self._config.env_file,
                )
                await client.connect()
                self._clients[chat_id] = client

            self._last_activity[chat_id] = datetime.now()
            return self._clients[chat_id]

    async def remove_client(self, chat_id: int):
        """Disconnect and remove a client."""
        async with self._lock:
            client = self._clients.pop(chat_id, None)
            self._last_activity.pop(chat_id, None)
            if client:
                await client.disconnect()

    async def _evict_oldest(self):
        """Evict the least recently used client to make room."""
        if not self._last_activity:
            return
        oldest_id = min(self._last_activity, key=self._last_activity.get)
        await self.remove_client(oldest_id)

    async def cleanup_idle(self, max_idle_minutes: int = 60):
        """Disconnect clients that have been idle too long."""
        now = datetime.now()
        to_remove = [
            cid for cid, last in self._last_activity.items()
            if (now - last).total_seconds() > max_idle_minutes * 60
        ]
        for cid in to_remove:
            await self.remove_client(cid)

    async def shutdown(self):
        """Disconnect all clients."""
        for chat_id in list(self._clients):
            await self.remove_client(chat_id)
```

### Event-Driven Response Rendering

Since jaato streams events, the Telegram client renders them progressively:

```python
async def handle_message(message: Message, pool: SessionPool):
    chat_id = message.chat.id
    client = await pool.get_client(chat_id)

    # Show typing indicator
    await message.answer_chat_action("typing")

    # Send user message to jaato
    await client.send_message(message.text)

    # Accumulate and render events
    response_text = ""
    sent_message = None
    last_edit_time = 0

    async for event in client.events():
        if event.type == EventType.AGENT_OUTPUT:
            response_text += event.content

            # Stream via edit-in-place (throttled)
            now = time.monotonic()
            if now - last_edit_time > 0.5:  # 500ms throttle
                if sent_message is None:
                    sent_message = await message.answer(response_text)
                else:
                    try:
                        await sent_message.edit_text(response_text)
                    except TelegramBadRequest:
                        pass  # Text unchanged
                last_edit_time = now

        # Handle other event types (permission requests, errors, etc.)
        # ...

    # Final edit with complete response
    if sent_message and response_text:
        try:
            await sent_message.edit_text(response_text)
        except TelegramBadRequest:
            pass
    elif response_text and sent_message is None:
        await message.answer(response_text)
```
---

## Message Types to Handle

**Inbound (Telegram -> jaato via SDK):**

| Telegram Input | Action |
|---|---|
| Text message | `await client.send_message(text)` |
| Photo/image with caption | Phase 3: download, encode, send as multimodal |
| Document/file | Phase 3: download, extract text, send |
| Voice message | Phase 3: transcribe externally, send text |
| Reply to bot message | Same session — just `send_message()` |
| `/start` command | Create SDK client in pool |
| `/reset` command | Disconnect + remove from pool, create fresh |
| `/status` command | Client-level status (NOT sent to jaato) |
| `/help` command | Client-level help (NOT sent to jaato) |

**Outbound (jaato events -> Telegram):**

| Event Type | Telegram Rendering |
|---|---|
| `EventType.AGENT_OUTPUT` | Accumulate text, edit-in-place or send_message |
| Permission request event | Inline keyboard (Approve / Approve All / Deny) |
| Error event | Error message to user |
| Tool execution event | Optional: show "Using tool: X..." status |
| Stream complete | Final edit with complete text |

**Long message handling:**

When accumulated response exceeds 4096 chars, split at paragraph boundaries and send as multiple messages. Only the last message uses edit-in-place for subsequent content.

### Permission Integration

When a jaato event signals a permission request, the client renders it as a Telegram inline keyboard:

```
Permission Request
Agent wants to execute: git push origin main

[Approve] [Approve All] [Deny]
```

The callback handler sends the decision back via the same SDK client's `send_message()` (or a dedicated permission method if the SDK exposes one).

### Group Chat Behavior

- Bot responds only when @mentioned or replied to (configurable)
- Each user in the group gets an isolated SDK client via `session_pool[chat_id-user_id]`
- Messages not directed at the bot are silently ignored
- Optional configurable prefix trigger (e.g., `!ask what is...`)
---

## Python SDK Selection

### Recommended: aiogram v3.x

**Package**: `pip install aiogram`

**Rationale**:

- Fully async (asyncio) — matches the async event streaming from jaato-sdk
- Router-based handler registration — clean mapping to `handlers/` package
- Built-in FSM — useful for permission approval flows
- Middleware support — rate limiting, auth checks, logging
- Modern Python 3.10+, comprehensive type hints
- Active development, Bot API 9.3+ support
- MIT license

**Key aiogram components**:

- `aiogram.Bot` — low-level Telegram API calls
- `aiogram.Dispatcher` — top-level event routing
- `aiogram.Router` — modular handler registration
- `aiogram.types.InlineKeyboardMarkup` / `InlineKeyboardButton` — permission buttons
- `aiogram.filters.Command` — command handlers
- `aiogram.filters.F` — magic filter for declarative matching
- `aiogram.fsm.context.FSMContext` — state machine for multi-step flows

### Not recommended

- **python-telegram-bot** — LGPL-3, historically sync-first
- **Telethon** — MTProto user client, wrong abstraction
- **Pyrogram** — unnecessarily complex for bot-only use
- **pyTelegramBotAPI** — synchronous by default

---

## Dependencies

### pyproject.toml

```toml
[project]
name = "jaato-client-telegram"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "jaato-sdk",                     # THE interface to jaato — nothing else
    "aiogram>=3.15",
    "aiohttp>=3.9",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "pyyaml>=6.0",
    "structlog>=24.0",
]

[project.optional-dependencies]
multimodal = [
    "aiofiles>=24.0",
    "Pillow>=10.0",
]
observability = [
    "opentelemetry-api>=1.20",
    "opentelemetry-sdk>=1.20",
]

[project.scripts]
jaato-tg = "jaato_client_telegram.__main__:main"
```

Note: `jaato-sdk` is the ONLY jaato dependency. The client never depends on or imports from the jaato server package.
---

## Implementation Phases

### Phase 1: Minimal Viable Client

- Text-only: `send_message()` -> accumulate `AGENT_OUTPUT` events -> `send_message()`
- Session pool: one `IPCRecoveryClient` per `chat_id`
- `/start` (create client) and `/reset` (recreate client) commands
- Long message splitting at paragraph boundaries
- Typing indicator while events stream
- Polling mode only
- YAML config loading with pydantic-settings

### Phase 2: Streaming + Interactive

- Edit-in-place streaming (send initial message, edit as events arrive)
- Edit throttling (respect Telegram rate limits, ~30 edits/min/chat)
- Permission approval via inline keyboards + callback handlers
- Group chat support with mention filtering
- Webhook mode
- `/help` command
- Idle session cleanup (background task)
- Graceful shutdown (disconnect all pool clients)

### Phase 3: Multimodal + Advanced

- Inbound image handling (download from Telegram, forward to jaato)
- Voice message transcription (external STT -> text)
- File upload processing
- OpenTelemetry span emission from client layer
- `/status` showing active sessions, connection states
- Rate limiting per chat_id
- Abuse protection (max message rate, max concurrent sessions)
---

## Reference Code: Phase 1 Skeleton

### Entry Point

```python
"""jaato_client_telegram/__main__.py"""

import asyncio
import logging
from jaato_client_telegram.config import load_config
from jaato_client_telegram.bot import create_bot_and_dispatcher
from jaato_client_telegram.session_pool import SessionPool

logger = logging.getLogger(__name__)


async def run():
    config = load_config()

    # Create session pool (manages SDK clients)
    pool = SessionPool(
        config=config.jaato,
        max_concurrent=config.session.max_concurrent,
    )

    # Set up Telegram bot
    bot, dp = create_bot_and_dispatcher(config, pool)

    logger.info("Starting jaato-client-telegram in %s mode", config.telegram.mode)
    try:
        if config.telegram.mode == "polling":
            await dp.start_polling(bot)
        else:
            # webhook setup
            ...
    finally:
        await pool.shutdown()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
```

### Session Pool

```python
"""jaato_client_telegram/session_pool.py"""

import asyncio
from datetime import datetime
from jaato_sdk.client import IPCRecoveryClient, ConnectionState


class SessionPool:
    """One IPCRecoveryClient per Telegram chat_id."""

    def __init__(self, config, max_concurrent: int = 50):
        self._config = config
        self._max_concurrent = max_concurrent
        self._clients: dict[int, IPCRecoveryClient] = {}
        self._last_activity: dict[int, datetime] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, chat_id: int) -> IPCRecoveryClient:
        async with self._lock:
            if chat_id not in self._clients:
                if len(self._clients) >= self._max_concurrent:
                    await self._evict_oldest()
                client = IPCRecoveryClient(
                    socket_path=self._config.socket_path,
                    auto_start=self._config.auto_start,
                    env_file=self._config.env_file,
                )
                await client.connect()
                self._clients[chat_id] = client
            self._last_activity[chat_id] = datetime.now()
            return self._clients[chat_id]

    async def remove_client(self, chat_id: int):
        async with self._lock:
            client = self._clients.pop(chat_id, None)
            self._last_activity.pop(chat_id, None)
            if client:
                await client.disconnect()

    async def _evict_oldest(self):
        if not self._last_activity:
            return
        oldest = min(self._last_activity, key=self._last_activity.get)
        # Release lock briefly for disconnect
        client = self._clients.pop(oldest, None)
        self._last_activity.pop(oldest, None)
        if client:
            await client.disconnect()

    async def shutdown(self):
        for chat_id in list(self._clients):
            await self.remove_client(chat_id)
```

### Private Chat Handler

```python
"""jaato_client_telegram/handlers/private.py"""

import time
from aiogram import Router, F
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest
from jaato_sdk.events import EventType
from jaato_client_telegram.session_pool import SessionPool
from jaato_client_telegram.renderer import split_preserving_paragraphs

router = Router()


@router.message(F.text, F.chat.type == "private")
async def handle_private_message(message: Message, pool: SessionPool):
    chat_id = message.chat.id
    client = await pool.get_client(chat_id)

    # Show typing
    await message.answer_chat_action("typing")

    # Send to jaato
    await client.send_message(message.text)

    # Accumulate streamed response
    response_text = ""
    sent_message = None
    last_edit = 0.0
    THROTTLE = 0.5  # seconds

    async for event in client.events():
        if event.type == EventType.AGENT_OUTPUT:
            response_text += event.content

            now = time.monotonic()
            if now - last_edit > THROTTLE:
                # Keep within Telegram limits
                display = response_text[:4096]
                if sent_message is None:
                    sent_message = await message.answer(display)
                else:
                    try:
                        await sent_message.edit_text(display)
                    except TelegramBadRequest:
                        pass
                last_edit = now

    # Final render
    if not response_text:
        return

    if len(response_text) <= 4096:
        if sent_message:
            try:
                await sent_message.edit_text(response_text)
            except TelegramBadRequest:
                pass
        else:
            await message.answer(response_text)
    else:
        # Delete streaming message, send split
        if sent_message:
            try:
                await sent_message.delete()
            except TelegramBadRequest:
                pass
        for chunk in split_preserving_paragraphs(response_text, 4096):
            await message.answer(chunk)
```

### Command Handlers

```python
"""jaato_client_telegram/handlers/commands.py"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from jaato_client_telegram.session_pool import SessionPool

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, pool: SessionPool):
    chat_id = message.chat.id
    # Ensure client exists
    await pool.get_client(chat_id)
    await message.answer(
        "Connected to jaato. Send me a message and I'll forward it to the agent."
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, pool: SessionPool):
    chat_id = message.chat.id
    await pool.remove_client(chat_id)
    await message.answer("Session reset. Send a new message to start fresh.")


@router.message(Command("status"))
async def cmd_status(message: Message, pool: SessionPool):
    active = len(pool._clients)
    await message.answer(f"Active sessions: {active}")
```

### Response Renderer

```python
"""jaato_client_telegram/renderer.py"""


def split_preserving_paragraphs(text: str, max_len: int) -> list[str]:
    """Split text on paragraph boundaries without exceeding max_len."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) > max_len:
                for i in range(0, len(para), max_len):
                    chunks.append(para[i : i + max_len])
                current = ""
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks
```
---

## Key Design Decisions

### 1. One SDK client per user (resolved)

The jaato SDK's `IPCRecoveryClient` represents a single connection with server-managed session state. Multi-user support requires multiple client instances. The `SessionPool` manages this mapping.

**Implication**: The jaato server must support multiple simultaneous IPC connections. If it currently assumes a single client, this needs to be addressed server-side.

### 2. Streaming strategy (resolved)

Streaming is native to the SDK — events arrive incrementally via `async for event in client.events()`. The Telegram client uses edit-in-place:

1. On first `AGENT_OUTPUT` event → `send_message()` (creates the message)
2. On subsequent events → `edit_message_text()` (updates in place)
3. Throttle edits to ~2/second (Telegram rate limit is ~30/minute/chat)
4. On stream complete → final `edit_message_text()` with full response
5. If response exceeds 4096 chars → delete streaming message, send split chunks

### 3. No bridge abstraction (resolved)

The previous design had a `bridge.py` with `JaatoBridge` ABC, `DirectBridge`, future `IPCBridge`/`HTTPBridge`. This is wrong. The `jaato-sdk` IS the bridge. It already handles transport, reconnection, and serialization. Adding another abstraction layer on top would be:
- Redundant (duplicating what the SDK does)
- Leaky (the bridge would need to expose the SDK's event streaming model anyway)
- Maintenance burden (two abstractions to keep in sync)

The handlers call the SDK directly. If the SDK's transport changes, only the SDK changes.

### 4. Session identity

The SDK client manages session state server-side. The Telegram client doesn't pass session IDs — instead, each Telegram user gets a dedicated SDK connection, and the server treats each connection as a separate session.

For group chats, the pool key includes both chat_id and user_id: `chat_id * 1_000_000 + user_id` or a tuple `(chat_id, user_id)`.

### 5. Client-side state

Minimal. The client only tracks:
- Session pool: `dict[int, IPCRecoveryClient]` + LRU timestamps
- Streaming state: current `message_id` being edited (ephemeral, per-handler)
- Callback state: pending permission decisions (in-memory, lost on restart is OK)

All real state lives in the jaato server's session system.
---

## References

- **jaato repository**: https://github.com/apanoia/jaato
- **jaato documentation**: https://apanoia.github.io/jaato/
- **jaato-sdk reference client**: https://github.com/apanoia/enphase-energy-monitoring (see `src/jaato_advisor.py`)
- **jaato-sdk API**: `jaato_sdk.client.IPCRecoveryClient`, `jaato_sdk.events.Event`, `jaato_sdk.events.EventType`
- **jaato plugins**: session, subagent, permission, gc, mcp, cli, file_edit, multimodal, web_search, background, clarification, references, slash_command, todo
- **jaato enterprise features**: OpenTelemetry, Kerberos/SPNEGO, IPC recovery, permission system, multi-provider abstraction
- **aiogram documentation**: https://docs.aiogram.dev/
- **aiogram GitHub**: https://github.com/aiogram/aiogram
- **Telegram Bot API**: https://core.telegram.org/bots/api
- **OpenClaw** (comparative reference for messaging-as-UI patterns): https://docs.openclaw.ai/
