


# IPC → WebSocket Migration Plan

**Status**: Draft  
**Date**: 2025-07-21  
**Scope**: Transport layer migration for `jaato-client-telegram`

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Current Architecture (IPC)](#2-current-architecture-ipc)
3. [Target Architecture (WebSocket)](#3-target-architecture-websocket)
4. [Interface Contract](#4-interface-contract)
5. [Migration Phases](#5-migration-phases)
6. [File-by-File Change Map](#6-file-by-file-change-map)
7. [Risk Assessment](#7-risk-assessment)
8. [Testing Strategy](#8-testing-strategy)
9. [Rollback Plan](#9-rollback-plan)

---

## 1. Motivation

The current implementation uses `IPCRecoveryClient` (Unix domain socket) to connect to `jaato-server`. This works for local development but has limitations for a multi-user public-facing Telegram bot:

| Concern | IPC (current) | WebSocket (target) |
|---------|--------------|---------------------|
| **Sandboxing** | Filesystem directories only | AppArmor kernel-enforced isolation |
| **Workspace provisioning** | Client copies templates manually | Server provisions from templates |
| **Transport security** | Filesystem permissions | TLS (`wss://`) |
| **Connection model** | N concurrent IPC sockets (one per user) | 1 shared WS connection |
| **Reconnection** | Per-client exponential backoff | Single WS reconnection |
| **File staging** | Upload roundtrip via separate path | Base64 in WS envelope |
| **Workspace reaping** | Client-side idle cleanup task | Server auto-reaps after `workspace_max_age` |
| **Path traversal** | Client controls filesystem paths | Server-side validation |

The **primary driver** is AppArmor sandboxing. A public Telegram bot lets untrusted users trigger arbitrary agent tool execution (file writes, shell commands, subprocesses). Kernel-enforced confinement is the only way to safely isolate workspaces.
---

## 2. Current Architecture (IPC)

```
Telegram User
    |
    v
handlers/private.py (or group.py)
    |  pool.get_client(chat_id) -> IPCRecoveryClient
    |  client.send_message(text)
    |  async for event in client.events():
    |      renderer.stream_response(message, event_stream)
    v
session_pool.py
    |  SessionPool manages dict[int, SessionInfo]
    |  SessionInfo holds IPCRecoveryClient + workspace_path
    |  Each client has its own socket connection
    v
workspace.py
    |  WorkspaceManager creates per-user directories
    |  Copies .env, .jaato/ (selective) from templates
    v
jaato_sdk.client.IPCRecoveryClient
    |  Unix domain socket to /tmp/jaato.sock
    |  Per-client reconnection with exponential backoff
    v
jaato-server
```

### Key data structures

```python
# session_pool.py
@dataclass
class SessionInfo:
    client: IPCRecoveryClient
    created_at: datetime
    last_activity: datetime
    workspace_path: str

class SessionPool:
    _sessions: dict[int, SessionInfo]  # chat_id -> session
```

### Handler interaction pattern (current)

```python
# handlers/private.py
client = await pool.get_client(chat_id)
await client.send_message(user_text)
await renderer.stream_response(initial_message=message, event_stream=client.events())

# handlers/callbacks.py
client = await pool.get_client(chat_id)
await client.respond_to_permission(request_id=request_id, response=option_key)
```

### What the renderer consumes

```python
# renderer.py -- stream_response() signature
async def stream_response(
    self,
    initial_message: Message,
    event_stream: AsyncIterator,  # client.events()
) -> None:
```

The renderer iterates events via `async for event in event_stream` and checks `event.type` (string) against known values like `"agent.output"`, `"tool.call_start"`, etc. It does **not** import SDK types -- it works with raw event dicts/objects.

---

## 3. Target Architecture (WebSocket)

```
Telegram User
    |
    v
handlers/private.py (or group.py)
    |  session_id = await pool.get_or_create_session(chat_id)
    |  await pool.send_message(session_id, text)
    |  async for event in pool.events(session_id):
    |      renderer.stream_response(message, event_stream)
    v
session_pool.py (rewritten)
    |  SessionPool manages dict[int, SessionMetadata]
    |  SessionMetadata holds session_id + last_activity
    |  Single WSTransport connection shared across all sessions
    |  Event dispatch by session_id from shared stream
    v
transport.py (new)
    |  WSTransport manages single WS connection
    |  Serializes/deserializes via jaato_sdk event protocol
    |  Dispatches events to per-session asyncio.Queue
    |  Handles reconnection, TLS, auth
    v
jaato_sdk event protocol (serialize_event / deserialize_event)
    |  Shared JSON event format over WS text frames
    v
JaatoWSServer (jaato-server)
    |  Workspace provisioning, AppArmor, TLS
    |  Multi-client session management
    v
jaato-server
```

### Key data structures (target)

```python
# session_pool.py
@dataclass
class SessionMetadata:
    session_id: str
    created_at: datetime
    last_activity: datetime

class SessionPool:
    _sessions: dict[int, SessionMetadata]  # chat_id -> metadata
    _transport: WSTransport               # single shared connection

# transport.py
class WSTransport:
    _ws: websockets.WebSocketClientProtocol
    _connected: bool
    _event_queues: dict[str, asyncio.Queue]  # session_id -> queue
    _receiver_task: asyncio.Task
```

### Handler interaction pattern (target)

```python
# handlers/private.py
session_id = await pool.get_or_create_session(chat_id)
await pool.send_message(session_id, user_text)
await renderer.stream_response(initial_message=message, event_stream=pool.events(session_id))

# handlers/callbacks.py
session_id = pool.get_session_id(chat_id)
await pool.respond_to_permission(session_id, request_id=request_id, response=option_key)
```

### Critical design decision: event dispatch

The WS server sends events for all sessions multiplexed on a single connection. Each event contains a `session_id` field. The `WSTransport` must:

1. Run a background receiver task that reads all WS messages
2. Deserialize each message via `deserialize_event()`
3. Route events to the correct `asyncio.Queue` based on `session_id`
4. Handlers consume events from their session's queue via `pool.events(session_id)`

```python
# transport.py -- receiver loop (conceptual)
async def _receiver_loop(self):
    async for raw_message in self._ws:
        event = deserialize_event(raw_message)
        session_id = event.session_id
        queue = self._event_queues.get(session_id)
        if queue:
            await queue.put(event)
```
---

## 4. Interface Contract

The `SessionPool` exposes these methods to handlers and renderer:

| Method | Current signature | Target signature | Notes |
|--------|-------------------|-----------------|-------|
| `get_client` | `async (chat_id) -> IPCRecoveryClient` | **Removed** | Replaced by `get_or_create_session` |
| `get_or_create_session` | *(new)* | `async (chat_id) -> str` | Returns `session_id` |
| `get_session_id` | *(new)* | `(chat_id) -> str or None` | Synchronous lookup |
| `send_message` | `client.send_message(text)` | `async pool.send_message(session_id, text)` | Pool-level method |
| `respond_to_permission` | `client.respond_to_permission(...)` | `async pool.respond_to_permission(session_id, ...)` | Pool-level method |
| `respond_to_clarification` | `client.respond_to_clarification(...)` | `async pool.respond_to_clarification(session_id, ...)` | Pool-level method |
| `events` | `client.events()` | `pool.events(session_id) -> AsyncIterator` | Returns async iterator over queue |
| `stop` | `client.stop()` | `async pool.stop(session_id)` | Stop agent execution |
| `remove_client` | `async (chat_id)` | `async (chat_id)` | Same semantics, different internals |
| `get_session_info` | `(chat_id) -> SessionInfo` | `(chat_id) -> SessionMetadata` | No `client` field |
| `active_count` | `-> int` | `-> int` | Unchanged |
| `cleanup_idle` | `async (max_idle_minutes) -> int` | Same | Unchanged signature |
| `shutdown` | `async ()` | `async ()` | Closes WS connection instead of N sockets |

### Renderer contract (unchanged)

The renderer's `stream_response()` takes an `AsyncIterator` of events. The iterator protocol stays the same -- the implementation changes from `IPCRecoveryClient.events()` to a queue-based async generator. **Zero changes to `renderer.py`**.

### What the renderer needs from events

The renderer accesses these attributes on each event:
- `event.type` -- string like `"agent.output"`, `"tool.call_start"`, `"permission.input"`
- `event.text` -- on `agent.output` events
- `event.tool_name` -- on tool events
- `event.content` -- on various events
- `event.request_id` -- on permission/clarification events
- `event.response_options` -- on permission events
- `event.options` -- on clarification events

These are all fields on the SDK event dataclasses. As long as `deserialize_event()` produces the same dataclass instances, the renderer works without modification.

---

## 5. Migration Phases

### Phase 1: Create `WSTransport` (new file)

**New file**: `src/jaato_client_telegram/transport.py`

Responsibilities:
- Connect to `JaatoWSServer` via `websockets` library
- Send events via `serialize_event()`
- Receive events via background task, deserialize via `deserialize_event()`
- Route events to per-session `asyncio.Queue`
- Handle reconnection with exponential backoff
- TLS support via `ssl.SSLContext`
- Auth token in handshake headers (`Authorization: Bearer <token>`)

```python
class WSTransport:
    def __init__(self, url, tls_config=None, secret_token=None):
        self._url = url
        self._session_queues: dict[str, asyncio.Queue] = {}
        self._ws = None
        self._receiver_task = None
        self._connected = False

    async def connect(self): ...
    async def disconnect(self): ...
    async def send(self, event):
        payload = serialize_event(event)
        await self._ws.send(payload)

    def register_session(self, session_id) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._session_queues[session_id] = queue
        return queue

    def unregister_session(self, session_id):
        self._session_queues.pop(session_id, None)

    async def events(self, session_id) -> AsyncIterator:
        queue = self._session_queues.get(session_id)
        if not queue: return
        while True:
            event = await queue.get()
            if event is None: break  # sentinel
            yield event

    async def _receiver_loop(self):
        async for raw in self._ws:
            event = deserialize_event(raw)
            sid = getattr(event, 'session_id', None)
            if sid and sid in self._session_queues:
                await self._session_queues[sid].put(event)
```

**Config addition** to `config.py`:

```python
class TLSConfig(BaseModel):
    enabled: bool = False
    cert_path: str | None = None
    key_path: str | None = None
    ca_cert_path: str | None = None

class JaatoWSConfig(BaseModel):
    url: str = 'ws://localhost:8080'
    tls: TLSConfig = Field(default_factory=TLSConfig)
    secret_token: str | None = None
    workspace_template: str = 'default'
```

**Dependencies**: Add `websockets` to `pyproject.toml`.

---

### Phase 2: Rewrite `session_pool.py`

Core change: pool switches from managing `IPCRecoveryClient` instances to routing through `WSTransport`.

Changes:
- Remove `IPCRecoveryClient` import, add `WSTransport`
- `SessionInfo` -> `SessionMetadata` (no `client` field)
- `get_client()` -> `get_or_create_session()` returning `session_id: str`
- New pool-level methods: `send_message()`, `respond_to_permission()`, `respond_to_clarification()`, `events()`, `stop()`
- Remove `WorkspaceManager` dependency (server handles provisioning)
- Presentation context sent in session creation request
- `shutdown()` closes WS connection instead of N sockets

```python
@dataclass
class SessionMetadata:
    session_id: str
    created_at: datetime
    last_activity: datetime

class SessionPool:
    def __init__(self, transport: WSTransport, max_concurrent: int = 50):
        self._transport = transport
        self._sessions: dict[int, SessionMetadata] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_session(self, chat_id: int) -> str:
        async with self._lock:
            if chat_id in self._sessions:
                return self._sessions[chat_id].session_id
            request = SessionNewRequest(
                name=f'telegram-{chat_id}',
                template=self._template,
                presentation=create_telegram_presentation_context(),
            )
            await self._transport.send(request)
            session_id = await self._wait_for_session_id()
            self._transport.register_session(session_id)
            self._sessions[chat_id] = SessionMetadata(
                session_id=session_id, created_at=datetime.now(),
                last_activity=datetime.now()
            )
            return session_id

    async def send_message(self, session_id, text):
        await self._transport.send(SendMessageRequest(text=text, session_id=session_id))

    async def events(self, session_id):
        return self._transport.events(session_id)

    async def respond_to_permission(self, session_id, request_id, response):
        await self._transport.send(PermissionResponseRequest(
            session_id=session_id, request_id=request_id, response=response
        ))
```

---

### Phase 3: Update handler files

Small, mechanical changes across 3 handler files.

#### `handlers/private.py`
```diff
- client = await pool.get_client(chat_id)
- await client.send_message(user_text)
- await renderer.stream_response(message, event_stream=client.events())
+ session_id = await pool.get_or_create_session(chat_id)
+ await pool.send_message(session_id, user_text)
+ await renderer.stream_response(message, event_stream=pool.events(session_id))
```

#### `handlers/group.py` -- same pattern as private.py

#### `handlers/callbacks.py`
```diff
- client = await pool.get_client(chat_id)
- await client.respond_to_permission(request_id=request_id, response=option_key)
+ session_id = pool.get_session_id(chat_id)
+ await pool.respond_to_permission(session_id, request_id=request_id, response=option_key)
```

---

### Phase 4: Update `bot.py` and `__main__.py`

#### `bot.py`
```diff
- workspace_manager = WorkspaceManager(config)
- pool = _create_session_pool(config, workspace_manager)
+ transport = WSTransport(
+     url=config.jaato_ws.url,
+     tls_config=config.jaato_ws.tls,
+     secret_token=config.jaato_ws.secret_token,
+ )
+ pool = SessionPool(transport=transport, max_concurrent=config.session.max_concurrent)
```

#### `__main__.py` -- `pool.shutdown()` call unchanged (internally closes WS)

---

### Phase 5: Remove IPC-specific code

- **Delete** `workspace.py` (236 lines) -- server handles provisioning
- **Remove** from `config.py`: `JaatoConfig.socket_path`, `.auto_start`, `.env_file`, `.workspace_path`
- **Remove** `WorkspaceManager` import from `bot.py`
- **Update** `__init__.py` docstring
- **Rewrite** `workspace_event_subscriber.py` for WS events (or remove)
- **Update** `config.example.yaml` -- replace IPC config with WS config

---

### Phase 6: Security hardening

- Enable TLS on the WS connection (`wss://`)
- Configure `secret_token` for authentication
- Set `workspace_template` for server-side provisioning
- Verify AppArmor profiles are active on the server
- Set `workspace_max_age` on the server for automatic reaping

---

## 6. File-by-File Change Map

| File | Action | Scope | Lines affected (est.) |
|------|--------|-------|----------------------|
| `transport.py` | **New** | WS transport implementation | ~200 |
| `session_pool.py` | **Rewrite** | Core session management | ~200 (was 285) |
| `config.py` | **Modify** | Add `TLSConfig`, `JaatoWSConfig`; remove IPC fields | ~30 |
| `bot.py` | **Modify** | Wire `WSTransport` instead of `WorkspaceManager` | ~20 |
| `__main__.py` | **Modify** | Shutdown sequence (minor) | ~5 |
| `handlers/private.py` | **Modify** | `get_client` -> `get_or_create_session` | ~15 |
| `handlers/group.py` | **Modify** | Same as private.py | ~15 |
| `handlers/callbacks.py` | **Modify** | `get_client` -> `get_session_id` | ~10 |
| `__init__.py` | **Modify** | Update docstring | ~5 |
| `config.example.yaml` | **Modify** | Replace IPC config with WS config | ~20 |
| `pyproject.toml` | **Modify** | Add `websockets` dependency | ~2 |
| `workspace.py` | **Delete** | Server handles provisioning | -236 |
| `workspace_event_subscriber.py` | **Rewrite or delete** | WS event dispatch | ~100 or delete |
| `renderer.py` | **None** | Transport-agnostic | 0 |
| `permissions.py` | **None** | Transport-agnostic | 0 |
| `rate_limiter.py` | **None** | Transport-agnostic | 0 |
| `abuse_protection.py` | **None** | Transport-agnostic | 0 |
| `telemetry.py` | **None** | Transport-agnostic | 0 |
| `whitelist.py` | **None** | Transport-agnostic | 0 |
| `file_handler.py` | **None** | Transport-agnostic | 0 |
| `agent_response_tracker.py` | **None** | Transport-agnostic | 0 |
| `workspace_tracker.py` | **None** | Transport-agnostic | 0 |
| `handlers/filters.py` | **None** | Transport-agnostic | 0 |
| `handlers/lifecycle.py` | **None** | Transport-agnostic | 0 |
| `handlers/commands.py` | **None** | Transport-agnostic | 0 |
| `handlers/admin.py` | **None** | Transport-agnostic | 0 |

**Total**: ~500 lines of new/changed code, ~236 lines deleted. 17 files require zero changes.

---

## 7. Risk Assessment

### High risk

| Risk | Mitigation |
|------|-----------|
| **Event dispatch correctness** -- events routed to wrong session | Unit tests with concurrent sessions. Include `session_id` in every logged event. |
| **WS reconnection loses in-flight events** | Buffer events during reconnection. Send `session.attach` after reconnect to replay missed events. |
| **Session creation race condition** -- two handlers create session for same `chat_id` | The `asyncio.Lock` in `get_or_create_session` prevents this. |
| **Renderer breaks if event format changes** | The renderer works with raw attributes. As long as SDK event dataclasses don't change, it's safe. |

### Medium risk

| Risk | Mitigation |
|------|-----------|
| **WS server not available at startup** | Retry connection in background. Handlers return "reconnecting" message until connected. |
| **TLS certificate issues** | Clear error messages. Config validation at startup. |
| **Memory leak in event queues** | Unregister session queues when sessions are evicted. |

### Low risk

| Risk | Mitigation |
|------|-----------|
| **Performance regression from single WS connection** | WebSocket is designed for multiplexing. A single connection handles thousands of concurrent sessions. |
| **`websockets` library version compatibility** | Pin version in `pyproject.toml`. Test against target version. |

---

## 8. Testing Strategy

### Unit tests

| Test | What it verifies |
|------|-----------------|
| `test_transport_connect_disconnect` | WS connection lifecycle |
| `test_transport_send_receive` | Event serialization/deserialization round-trip |
| `test_transport_event_routing` | Events dispatched to correct session queue |
| `test_transport_reconnection` | Reconnect after disconnect, queues preserved |
| `test_session_pool_create` | Session creation via WS, queue registered |
| `test_session_pool_send_message` | Message sent through transport |
| `test_session_pool_events` | Events yielded from queue iterator |
| `test_session_pool_eviction` | Oldest session evicted, queue unregistered |
| `test_session_pool_concurrent` | Lock prevents duplicate session creation |

### Integration tests

| Test | What it verifies |
|------|-----------------|
| `test_handler_private_message_flow` | Full flow: handler -> pool -> transport -> mock WS server -> events -> renderer |
| `test_handler_permission_flow` | Permission callback -> pool -> transport -> mock server |
| `test_bot_startup_ws` | Bot starts, connects to WS, creates session on first message |
| `test_bot_shutdown` | Graceful shutdown closes WS connection |

### Manual testing

1. Start `jaato-server` with WS enabled
2. Start bot with WS config
3. Send message in private chat -- verify response streams correctly
4. Trigger permission request -- verify inline keyboard works
5. Kill WS connection -- verify reconnection
6. Send message in group chat -- verify shared session works

---

## 9. Rollback Plan

If the WS migration causes critical issues:

1. The old IPC code is preserved in git history (`git log -- session_pool.py`)
2. Revert to the pre-migration commit: `git checkout <pre-migration-sha> -- src/`
3. The renderer and handlers are transport-agnostic, so they work with either transport
4. The config file supports both `jaato` (IPC) and `jaato_ws` (WS) sections during transition

**Recommended**: Keep both config sections during development. Use a feature flag or config field (`transport: "ipc" | "ws"`) to switch at runtime until WS is validated.