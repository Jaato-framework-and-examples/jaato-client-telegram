# Migrating the bot onto the SDK facade WS client

Status: **in progress** (branch `feat/facade-ws-client`). Bulk scoped + de-risked
2026-06-30. Gated on two in-flight SDK changes from Advisor (see "SDK gates").

## Goal
Replace the hand-rolled `transport.py::WSTransport` with the SDK's facade WS
client (`WSRecoveryClient` — recoverable, auto-reconnect) so the bot rides
Advisor's maintained protocol handling and gains auto-reconnect. The end state
**ships recovery** (Daniel's call — not a WSClient stopgap).

## The seam: `SessionPool` internals only
Every caller goes through `SessionPool`'s public methods, so we keep that surface
**identical** and swap only the internals. Unchanged: `renderer.stream_response`,
`handlers/*`, `clarification.py`, `callbacks.py`. `SessionMetadata.transport` →
`SessionMetadata.client` (a `WSRecoveryClient`).

## Method mapping (WSTransport → facade client)
| today (WSTransport)                         | facade client (`WSRecoveryClient`)                          |
|---------------------------------------------|-------------------------------------------------------------|
| `connect()` + manual `ClientConfigRequest` + `set_workspace` | `connect()` (handshake sends presentation+workspace+config) |
| `register_host_tools(schemas, categories)`  | `register_client_tools([{**schema, "handler": exec}])`      |
| `set_session_tool_executors(...)`           | gone — `handler` in the tool dict; client dispatches it     |
| `_handle_tool_execute_request` (our loop)   | gone — client's `_on_tool_execute_request` (sync+async)     |
| `list_sessions() -> list[str]`              | `list_sessions() -> None` (event `SessionListEvent`) **or** just try-attach |
| `attach_session(id)` (fire-and-forget)      | `attach_session(id) -> bool` (True on success)              |
| `create_session(["--profile",p,"--agent",a])` | `create_session(profile=p, agent=a) -> session_id`        |
| `register_session(id)` / `events(id)`       | `events()` (one client = one session; no id)                |
| `send(SendMessageRequest(...))`             | `send_message(text, attachments=...)`                       |
| `send(PermissionResponseRequest(...))`      | `respond_to_permission(id, resp, edited_arguments=...)` ✅ matches our UI |
| `send(ClarificationBatchResponseEvent(...))`| **gap** — client has single `respond_to_clarification(id, str)`; chat uses batch |
| `send(StopRequest())`                       | `stop()`                                                    |
| `stage_files(...)`                          | DELETE — dead code (no caller)                              |
| `.connected`                                | `.is_connected` (recovery) / `.connected` (plain)           |
| `disconnect()`                              | `disconnect()`                                              |

## Construction
`WSRecoveryClient(url, token=secret_token, client_type=ClientType.CHAT,
ssl=<ctx>/ca=<path>, workspace_path=workspace,
config_root=<workspace>/.jaato, presentation=<telegram PresentationContext>)`.

Telegram presentation (must be sent — see gate 1):
`content_width=45, content_height=None, supports_markdown=True,
supports_tables=False, supports_code_blocks=True, supports_images=True,
supports_rich_text=True, supports_unicode=True, supports_mermaid=False,
supports_expandable_content=True, client_type=CHAT`.

## SDK gates (Advisor, in-flight 2026-06-30)
1. **presentation= hook** on WSClient/WSRecoveryClient + `session()` — REQUIRED,
   else multi-field rendering regression (tables on, images off, width 80,
   expandable off). Landing now (ipc.py done; recovery.py in progress).
2. **recovery proxying** — `IPCRecoveryClient`/`WSRecoveryClient` must proxy
   `register_client_tools` + `list_sessions` (absent today; no `__getattr__`).
   Without it `session(mode="ws", recovery=True, client_tools=...)` AttributeErrors.
   REQUIRED for a recoverable host-tool client. Landing now.
3. **batch clarification** (confirm) — blessed public way to send
   `ClarificationBatchResponseEvent`, or use `client._send_event(...)`.
4. **set_workspace** (confirm) — does `_handshake` send the 3rd wire, or do we
   call `client.execute_command("set_workspace",[ws])` post-connect?
5. **token form** (confirm) — server accepts `?token=` query for `secret_token`
   (WSClient form) vs our current `Authorization: Bearer` header.

## Verified facts (SDK, no longer assumptions)
- Persistent background drain task → `events()`/`subscribe`/`attach_session`
  work concurrently; no manual receiver loop needed.
- `respond_to_permission(id, response, edited_arguments=None)` — matches our UI.
- `create_session(...) -> Optional[str]` returns the id (waits for SessionInfo
  when `events()` isn't active — true during `get_or_create_session`).
- `register_client_tools` entry: `{name, description, parameters, handler,
  timeout?, auto_approve?}`; re-call = our `force=True` refresh. NOTE: it sends
  `categories={}` — we lose `TOOL_CATEGORIES` grouping (minor).

## Plan after gates land
1. Rewrite `SessionPool` internals per the table; delete `transport.py`.
2. `_assemble_host_tools` → produce `{**schema, "handler": executor}` dicts.
3. Re-attach: `attach_session(persisted)`; on False → `create_session`.
4. Map `TLSConfig` → `ssl=`/`ca=`.
5. Update/extend tests (mock the facade client). Keep old WSTransport tests'
   intent where still relevant.
6. Live cutover only after gates 1+2 land; recovery (auto-reconnect) verified.
