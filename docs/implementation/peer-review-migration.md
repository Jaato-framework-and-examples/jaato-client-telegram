# jaato-client-telegram — Peer Review & Migration Assessment

**Reviewer**: jaato agent (automated)  
**Date**: 2025-07-21  
**Version reviewed**: 0.1.0  
**Scope**: Full codebase (25 source files, ~270KB)  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Assessment](#2-architecture-assessment)
3. [Peer Review Findings](#3-peer-review-findings)
   - 3.1 Critical / High Severity
   - 3.2 Medium Severity
   - 3.3 Low Severity / Suggestions
4. [SDK Reference Alignment](#4-sdk-reference-alignment)
5. [IPC → WebSocket Migration Assessment](#5-ipc--websocket-migration-assessment)
   - 5.1 Why Migrate
   - 5.2 Impact by File
   - 5.3 Architectural Shift
   - 5.4 Migration Plan
6. [Appendix: Files Unchanged by Migration](#appendix-files-unchanged-by-migration)

---

## 1. Executive Summary

`jaato-client-telegram` is a Telegram bot that bridges Telegram conversations to a `jaato-server` daemon. Each user gets an isolated AI agent session with per-user workspace directories. The bot is built on aiogram 3.x and uses `jaato-sdk`'s `IPCRecoveryClient` for server communication.

**Overall verdict**: Solid implementation, production-candidate with issues to address. The architecture is clean, handler decomposition is good, and the Telegram-side infrastructure (permissions, rate limiting, abuse protection, telemetry, whitelist) is comprehensive and transport-agnostic.

**Key recommendation**: Migrate from IPC to WebSocket transport. This enables AppArmor sandboxing (kernel-enforced workspace isolation), server-side workspace provisioning, and TLS — all of which are important for a multi-user public-facing bot where untrusted users trigger arbitrary agent tool execution.

---

## 2. Architecture Assessment

### Layered Architecture — Well Done

```
handlers/ → renderer.py → session_pool.py → jaato-sdk IPCRecoveryClient → jaato-server
```

The layers are clean and match the jaato client reference model:

| Layer | Responsibility | Assessment |
|-------|---------------|------------|
| `handlers/` | Telegram message routing (private, group, admin, callbacks, lifecycle) | Clean router separation |
| `renderer.py` | Streaming event rendering to Telegram messages | Comprehensive, handles flush, permissions, tool output, expandable blockquotes |
| `session_pool.py` | Per-user SDK client lifecycle management | Good isolation model, correct reconnection pattern |
| `workspace.py` | Per-user filesystem isolation | Thoughtful selective `.jaato/` copying |
| `config.py` | YAML config with `${VAR}` env substitution | Clean Pydantic models |
| `permissions.py` | Permission approval UI with inline keyboards | Well-structured, filters unsupported action types |
| `rate_limiter.py` | Token bucket rate limiting | Correct algorithm, admin bypass, cleanup task |
| `abuse_protection.py` | Suspicion scoring, reputation, graduated bans | Comprehensive |
| `telemetry.py` | Bot-layer metrics (Telegram delivery, UI interactions, latency) | Good observability foundation |
| `whitelist.py` | Username-based access control with approval workflow | Mature, admin notifications |

### Presentation Context — Correct

The bot correctly identifies itself as a `chat` client type with appropriate constraints:

```python
{
    "content_width": 45,           # Mobile Telegram width
    "supports_markdown": True,     # Basic markdown
    "supports_tables": False,      # Tables don't render on mobile
    "supports_expandable_content": True,  # Custom handling via blockquotes
    "client_type": "chat",         # Messaging platform
}
```

This matches the SDK reference's `ClientType.CHAT` with the implied `CommunicationStyle.CONVERSATIONAL`.

---

## 3. Peer Review Findings

### 3.1 Critical / High Severity

#### 🔴 C1: User lock dicts grow without bound — memory leak

**Files**: `handlers/private.py:28`, `handlers/group.py:28`

```python
_user_locks: dict[int, asyncio.Lock] = {}
```

Both handler modules maintain a module-level dict of per-user locks that is **never cleaned up**. Every unique `chat_id`/`user_id` that sends a message creates a permanent `asyncio.Lock` entry. Over weeks/months of operation, this leaks memory proportional to **total unique users**, not active users.

**Impact**: Unbounded memory growth. A bot serving 10K unique users over its lifetime retains 10K lock objects permanently.

**Fix**: Use `WeakValueDictionary`, or store locks in `SessionPool` (which already tracks active users and evicts stale ones), or clean up locks when sessions are evicted.

---

#### 🔴 C2: Event iterator consumed once — breaks multi-turn agentic flows

**File**: `renderer.py` — `stream_response()` method

The renderer calls `async for event in event_stream:` which exhausts the `client.events()` iterator. Per the SDK reference, `events()` is a **single-use async generator** protected by the `_events_active` flag (concurrent reader prevention). If `stream_response` is called a second time for the same client — e.g., after a permission approval resumes the flow — `event_stream` will be empty.

The SDK reference explicitly states:

> The `_events_active` flag prevents concurrent reads on the `StreamReader` — when `events()` is actively iterating, `create_session()` falls back to fire-and-forget mode.

**Impact**: Multi-turn agentic flows (where the agent makes multiple tool calls across multiple turns) will silently drop all events after the first `stream_response` call returns.

**Fix**: The `StreamingContext` or `SessionPool` needs to own the event loop. Consider having `SessionPool` manage a long-running event consumer per client that dispatches events to the appropriate renderer instance, rather than passing the iterator to the caller.

---

#### 🔴 C3: `client.send_event()` may not exist on `IPCRecoveryClient`

**File**: `session_pool.py:127-135`

```python
config_event = ClientConfigRequest(presentation=presentation_ctx)
await client.send_event(config_event)
```

The SDK reference documents these public methods on `IPCRecoveryClient`: `send_message()`, `respond_to_permission()`, `respond_to_clarification()`, `stop()`, `execute_command()`, `events()`. The method `send_event()` is **not listed**. If it doesn't exist, the presentation context is never sent.

The error is silently swallowed:

```python
except Exception:
    # Log but don't fail - presentation is optional
    pass
```

**Impact**: The server never receives the bot's display capabilities. The agent may output tables, wide code blocks, or mermaid diagrams that render poorly on Telegram.

**Fix**: Verify the SDK API. If `send_event` doesn't exist, use the correct method. Replace the bare `except Exception: pass` with at minimum a logged warning.

---

#### 🔴 C4: Dead code — duplicate `except` block in `FileHandler`

**File**: `file_handler.py:89-98`

```python
        except Exception as e:
            logger.exception(f"Error sending file: {e}")
            await message.answer(...)
            return False

        except Exception as e:   # ← unreachable
            logger.exception(f"Error handling file event: {e}")
            await message.answer(...)
            return False
```

The second `except` block is unreachable dead code. This is likely a merge artifact.

**Impact**: No runtime impact, but indicates a broken merge that may have lost logic.

**Fix**: Remove the dead block. Verify the intended behavior was preserved.

---

#### 🔴 C5: Group session isolation — documentation contradicts code

**File**: `handlers/group.py`

The docstring says:

> Each user gets their own isolated session even within groups.

And the rate limiting uses `user_id`:

```python
await abuse_protector.check_message(user_id=user_id, ...)
await rate_limiter.check_rate_limit(user_id=user_id, ...)
```

But the actual session creation uses the **group chat_id**:

```python
client = await pool.get_client(message.chat.id)  # group-level, not user-level
```

All group members share a single session and conversation history.

**Impact**: Misleading documentation. Users in a group may assume their conversations are private when they are not. The shared session may be intentional (collective group memory), but the help text and docstrings claim otherwise.

**Fix**: Decide on the semantics (per-user vs per-group) and update both code and all documentation consistently. The `/help` command in group mode says "Each user gets their own isolated session" — this must match the actual behavior.

---

### 3.2 Medium Severity

#### 🟡 M1: String comparisons for event types instead of `EventType` enum

**File**: `renderer.py` — throughout `stream_response()`

```python
if event_type == "agent.output" or event_type == "AGENT_OUTPUT":
```

The SDK provides an `EventType` enum. The code extracts the type as a string via `getattr` then compares against both lowercase and uppercase literals. This is fragile — if enum values change or new event types are added, the renderer silently ignores them.

**Fix**: Import `EventType` from `jaato_sdk.events` and compare against the enum members directly.

---

#### 🟡 M2: `_edit_or_send` doesn't handle HTML parse errors on initial send

**File**: `renderer.py`

When `has_html` is `True`, the code sends with `parse_mode="HTML"`. The edit path catches `TelegramBadRequest`, but the initial `answer()` call does not. Unescaped HTML in accumulated text will cause an unhandled exception.

**Fix**: Wrap the initial `answer()` call in the same `try/except TelegramBadRequest` block.

---

#### 🟡 M3: Admin commands lack `admin_user_ids` verification

**File**: `handlers/admin.py`

Commands like `/whitelist_add`, `/ban`, `/unban` check `whitelist.is_admin(username)` but do NOT verify against `config.telegram.access.admin_user_ids`. The admin check is solely username-based from the whitelist JSON file.

If the whitelist file is compromised or misconfigured, any user listed as `admin_username` gains full admin access regardless of the Telegram config's `admin_user_ids`.

**Fix**: Add a secondary check: `if user_id not in admin_user_ids: deny`.

---

#### 🟡 M4: `MentionedMe` filter calls `bot.me()` on every message

**File**: `handlers/filters.py:56`

```python
me = await bot.me()
```

This makes an API call (or cache lookup) for every message in every group chat. While aiogram may cache this, the first message before cache warmup incurs a network round-trip.

**Fix**: Resolve the bot username once at startup and store it in `dp["bot_username"]`.

---

#### 🟡 M5: Whitelist file writes have no concurrency protection

**File**: `whitelist.py`

Every mutation method (`add_user`, `remove_user`, `approve_request`, etc.) calls `self.save()` which writes to JSON. Two concurrent admin operations (e.g., approving a request while another admin removes a user) can interleave writes and corrupt the file.

**Fix**: Use an `asyncio.Lock` around read-modify-write cycles in `WhitelistManager`.

---

#### 🟡 M6: `_send_as_document` doesn't close file handle

**File**: `file_handler.py:126`

```python
await message.answer_document(document=open(file_path, "rb"), ...)
```

The file is opened but never explicitly closed. Relying on aiogram to close it is fragile.

**Fix**: Use a context manager or `aiofiles`.

---

### 3.3 Low Severity / Suggestions

#### 🟢 L1: `workspace_event_subscriber.py` is dead code

The module is created as `None` in `bot.py` with a TODO comment. The entire 119-line file is unreachable.

**Suggestion**: Remove or gate behind a feature flag.

---

#### 🟢 L2: `workspace_tracker.py` has no persistence

If the bot restarts, all file tracking state is lost. Fine for now but should be documented.

---

#### 🟢 L3: Unused import in `config.py`

`pydantic_settings.BaseSettings` is imported but never used — `Config` extends `BaseModel`, not `BaseSettings`.

---

#### 🟢 L4: Idle cleanup task runs even when pool is empty

Minor efficiency issue — wakes every 30 minutes regardless of session count.

---

#### 🟢 L5: `_is_wide_content` has false positives

Checking for single backtick (`` ` ``) triggers "wide content" for any text containing inline code, even short snippets.

---

#### 🟢 L6: No handling of `RECONNECTING` state in `get_client`

When `get_client` returns a client in `RECONNECTING` state, `send_message()` raises `ReconnectingError`. This is caught by the generic `except` but produces a confusing user-facing error.

**Suggestion**: Check `client.is_reconnecting` and return a friendly "reconnecting, please wait" status.

---

#### 🟢 L7: ~25 markdown documentation files in project root

Files like `IMPLEMENTATION_SUMMARY.md`, `TELEMETRY_IMPLEMENTATION.md`, etc. appear to be AI-generated implementation notes. These should be in `docs/` or removed.

---

## 4. SDK Reference Alignment

Assessment against the `jaato-ipc-ws-transport-clients` reference:

| Aspect | Reference Requirement | Implementation | Status |
|--------|----------------------|----------------|--------|
| Uses `IPCRecoveryClient` | Required for IPC transport | ✅ Used correctly | ✅ Pass |
| `set_session_id()` for reconnection | Required after `create_session()` | ✅ Called at line 169 | ✅ Pass |
| Presentation context | Recommended for client adaptation | ⚠️ `send_event()` may not exist | ⚠️ Verify |
| `client_type: "chat"` | Correct for messaging platforms | ✅ Set correctly | ✅ Pass |
| `RecoveryConfig` layered config | Env → project → user → defaults | ❌ Not used; hard-coded constructor args | ⚠️ Gap |
| Event protocol via `EventType` enum | Required for forward compatibility | ❌ String comparisons | 🔴 Fail |
| Concurrent reader prevention | `_events_active` flag on `events()` | ❌ Iterator consumed once per call | 🔴 Fail |
| Error classification | Transient vs permanent errors | ❌ Not leveraged in handlers | ⚠️ Gap |
| Connection ghost prevention (Windows) | Required for pipe retry logic | N/A (Linux deployment assumed) | — |
| `IncompatibleServerError` handling | Non-retryable, stop reconnection | ❌ Not handled | ⚠️ Gap |

---

## 5. IPC → WebSocket Migration Assessment

### 5.1 Why Migrate

The current implementation uses `IPCRecoveryClient` (Unix domain socket) to connect to `jaato-server`. This provides **filesystem-level isolation only** — the server process runs with its full permissions, and "isolation" is just different working directories.

Migrating to `JaatoWSServer` (WebSocket transport) enables:

| Capability | IPC | WebSocket |
|-----------|-----|-----------|
| **AppArmor sandboxing** | ❌ Not available | ✅ Per-workspace kernel-enforced confinement |
| **Server-side workspace provisioning** | ❌ Client manages directories | ✅ Server provisions from templates |
| **Staged files** | ❌ Not available | ✅ Base64 files in WS envelope, no upload roundtrip |
| **TLS transport encryption** | ❌ Filesystem permissions only | ✅ `wss://` with cert/key/ca |
| **Workspace reaping** | ❌ Client-side idle cleanup | ✅ Server auto-reaps after `workspace_max_age` |
| **Path-traversal protection** | ❌ Client controls paths | ✅ Server-side validation |
| **Connection model** | N concurrent IPC sockets | 1 shared WS connection |

For a **multi-user public-facing Telegram bot** where untrusted users can trigger arbitrary agent tool execution (file writes, shell commands, subprocesses), AppArmor sandboxing is the single most important security improvement.

### 5.2 Impact by File

#### Files that must change significantly

##### `session_pool.py` — **Rewrite** (the core impact)

This is the biggest change. The entire pool is built around `IPCRecoveryClient` — one instance per user, each with its own socket connection, session lifecycle, and reconnection state.

**What changes:**
- `IPCRecoveryClient` → WS client connection to `JaatoWSServer`
- The pool no longer creates/manages SDK client instances — the **server** provisions workspaces and manages sessions
- `create_session()`, `set_session_id()`, `connect()`, `disconnect()` → replaced by WS messages (`session.new`, `message.send`, etc.) through a shared WS connection
- Reconnection shifts from per-client exponential backoff to **single WS connection** reconnection
- `SessionInfo` dataclass: remove `client: IPCRecoveryClient`, replace with `session_id` + `workspace_path` tracking
- `create_telegram_presentation_context()` stays the same — it's server-agnostic

**Current interface:**
```python
client = await pool.get_client(chat_id)
await client.send_message(text)
async for event in client.events():
    ...
await client.respond_to_permission(request_id, response)
```

**New interface:**
```python
session_id = await pool.get_or_create_session(chat_id)
await pool.send_message(session_id, text)
async for event in pool.events(session_id):
    ...
await pool.respond_to_permission(session_id, request_id, response)
```

---

##### `config.py` — **Modify `JaatoConfig`**

```python
# Current (IPC)
class JaatoConfig(BaseModel):
    socket_path: str = "/tmp/jaato.sock"
    auto_start: bool = False
    env_file: str = ".env"
    workspace_path: str = "workspaces"

# New (WebSocket)
class JaatoWSConfig(BaseModel):
    ws_url: str = "ws://localhost:8080"
    tls: bool = False
    cert_path: str | None = None
    ca_cert: str | None = None
    secret_token: str | None = None
    # Optional: request specific workspace template
    workspace_template: str = "default"
```

The `socket_path`, `auto_start`, `env_file` fields go away. The `workspace_path` field goes away — the **server** manages workspace provisioning.

---

##### `workspace.py` — **Delete or reduce to config holder**

The entire `WorkspaceManager` / `Workspace` class (236 lines) manages per-user directory creation, template copying, `.jaato/` selective copy, and cleanup. With WS provisioning, the **server** handles all of this.

This module either becomes a thin config holder (what template to request) or is removed entirely.

---

##### `workspace_event_subscriber.py` — **Rewrite** (already dead code)

Currently subscribes via IPC backend. With WS, file watching events arrive as WS messages. The `WorkspaceFileTracker` (in-memory dict) stays the same, but the subscriber mechanism changes to WS event dispatch.

---

##### `bot.py` — **Modify wiring**

Replace `SessionPool(config, workspace_manager)` construction. The pool no longer needs a `WorkspaceManager`. Add WS connection setup and lifecycle management.

---

##### `__main__.py` — **Modify shutdown**

Shutdown changes: instead of `pool.shutdown()` disconnecting N IPC clients, it closes a single WS connection and sends session detach messages.

---

##### `handlers/callbacks.py` — **Small change**

```python
# Current: get IPCRecoveryClient from pool, call SDK method
client = await pool.get_client(chat_id)
await client.respond_to_permission(request_id=request_id, response=option_key)

# New: send event through pool's WS connection
await pool.respond_to_permission(session_id, request_id=request_id, response=option_key)
```

---

##### `handlers/private.py` + `handlers/group.py` — **Small change**

```python
# Current
client = await pool.get_client(chat_id)
await client.send_message(user_text)
event_stream = client.events()

# New
session_id = await pool.get_or_create_session(chat_id)
await pool.send_message(session_id, user_text)
event_stream = pool.events(session_id)
```

---

##### `__init__.py` — **Trivial** (update docstring)

---

#### Files that do NOT change

See [Appendix](#appendix-files-unchanged-by-migration) for full list.

| File | Reason |
|------|--------|
| `renderer.py` | Receives events via async iterator — interface stays the same |
| `permissions.py` | Formats UI, parses callbacks — transport-agnostic |
| `rate_limiter.py` | Pure in-memory state — no transport dependency |
| `abuse_protection.py` | Pure in-memory state — no transport dependency |
| `telemetry.py` | Pure metrics collection — no transport dependency |
| `whitelist.py` | Pure access control — no transport dependency |
| `file_handler.py` | Sends files via Telegram API — no transport dependency |
| `agent_response_tracker.py` | Regex-based file detection — no transport dependency |
| `handlers/filters.py` | Telegram message filtering — no transport dependency |
| `handlers/lifecycle.py` | Bot join/leave events — no transport dependency |
| `handlers/commands.py` | Bot commands — no transport dependency |
| `handlers/admin.py` | Admin commands — no transport dependency |

### 5.3 Architectural Shift

The fundamental change is **who owns session lifecycle**:

| Aspect | Current (IPC) | New (WebSocket) |
|--------|--------------|-----------------|
| **Session creation** | Client calls `client.create_session()` | Client sends `session.new` WS message; server provisions workspace |
| **Workspace management** | Client creates dirs, copies templates, selective `.jaato/` copy | Server provisions from template with AppArmor |
| **Reconnection** | Per-client `IPCRecoveryClient` with exponential backoff | Single WS connection with reconnection |
| **Connection count** | N concurrent IPC sockets (one per user) | 1 WS connection |
| **Isolation enforcement** | Filesystem only (client-side directories) | AppArmor (kernel-enforced, server-side) |
| **Event streaming** | Per-client `client.events()` async iterator | Shared WS event stream, dispatched by `session_id` |
| **Presentation context** | Client sends via `send_event()` (possibly broken) | Sent in WS `session.new` envelope or `ClientConfigRequest` |
| **Template/env management** | Client copies `.env` and `.jaato/` to per-user dirs | Server handles during provisioning |
| **Idle cleanup** | Client-side `_idle_session_cleanup_task` | Server auto-reaps after `workspace_max_age` |

### 5.4 Migration Plan

#### Phase 1: Extract transport abstraction (lowest risk)

Create a `TransportClient` protocol/ABC that both `IPCTransport` and `WSTransport` implement:

```python
class TransportClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def send_message(self, session_id: str, text: str) -> None: ...
    async def respond_to_permission(self, session_id: str, request_id: str, response: str) -> None: ...
    async def events(self, session_id: str) -> AsyncIterator[Event]: ...
    async def create_session(self, name: str) -> str: ...
```

Then refactor `SessionPool` to depend on `TransportClient`, not `IPCRecoveryClient` directly. This allows switching transports via config without touching handlers or renderer.

#### Phase 2: Implement `WSTransport`

Create `WSTransport` that connects to `JaatoWSServer` and implements the `TransportClient` protocol. Key considerations:

- **Single WS connection** shared across all sessions
- **Event dispatch** by `session_id` from the shared event stream
- **TLS** via `ssl_context` if configured
- **Reconnection** at the WS level (single connection to manage)
- **Presentation context** sent in `session.new` envelope

#### Phase 3: Update config and wiring

- Add `JaatoWSConfig` to `config.py`
- Update `bot.py` to instantiate `WSTransport` when WS mode is configured
- Update `__main__.py` shutdown sequence

#### Phase 4: Remove IPC-specific code

- Delete `workspace.py` (server handles provisioning)
- Rewrite `workspace_event_subscriber.py` for WS events
- Remove IPC-specific config fields
- Update `__init__.py` docstring

#### Phase 5: Security hardening

- Enable AppArmor on the server
- Configure workspace templates with appropriate profiles
- Set `workspace_max_age` for automatic reaping
- Enable TLS for the WS connection

**Estimated scope**: ~500-600 lines of substantive changes across ~10 files. The renderer and all Telegram-side infrastructure (~12 files) require zero changes.

---

## Appendix: Files Unchanged by Migration

These files have no dependency on the transport layer and require zero changes:

| File | Lines | Role |
|------|-------|------|
| `renderer.py` | 930 | Event-to-Telegram message rendering |
| `permissions.py` | 399 | Permission approval UI |
| `rate_limiter.py` | 308 | Token bucket rate limiting |
| `abuse_protection.py` | 501 | Suspicion/reputation/ban system |
| `telemetry.py` | 366 | Bot-layer metrics |
| `whitelist.py` | 522 | Access control |
| `file_handler.py` | 183 | File sending to Telegram |
| `agent_response_tracker.py` | 152 | File mention detection |
| `workspace_tracker.py` | 122 | In-memory file registry |
| `handlers/filters.py` | 79 | Mention detection filter |
| `handlers/lifecycle.py` | 61 | Bot join/leave events |
| `handlers/commands.py` | 138 | /start, /reset, /help |
| `handlers/admin.py` | 931 | Admin commands |
| `handlers/callbacks.py` | 170 | Inline button handlers |
| `handlers/__init__.py` | 25 | Router exports |
| `config.py` | 235 | Config models (modify only) |
| `pyproject.toml` | 80 | Project metadata (add `websockets` dep) |

**Total unchanged**: ~5,122 lines across 17 files (config and pyproject get minor edits).
