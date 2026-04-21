# IPC Recovery Fix

## Problem

The `IPCRecoveryClient` handles socket-level reconnection automatically, but when the jaato-server connection drops and reconnects, the **server-side session is lost**. The client code in `SessionPool.get_client()` was returning cached clients without checking their connection state, causing `send_message()` to fail with a stale session ID.

### Root Cause

In the original implementation:

```python
# session_pool.py - get_client() method
if chat_id in self._sessions:
    session = self._sessions[chat_id]
    session.last_activity = now
    return session.client  # ← Returns client without checking state
```

When a jaato-server connection drop occurs:
1. Socket reconnects (IPC recovery works)
2. Server-side session is lost
3. Cached client still has old session_id
4. Next `send_message()` fails because server doesn't recognize the session

## Solution

### Changes to `session_pool.py`

1. **Import ConnectionState**:
   ```python
   from jaato_sdk.client import IPCRecoveryClient, ConnectionState
   ```

2. **Add logging**:
   ```python
   import logging
   logger = logging.getLogger(__name__)
   ```

3. **Add `_remove_client_only()` helper**:
   - Disconnects and removes a client from the pool
   - **Preserves the workspace directory** (doesn't call `cleanup_workspace`)
   - Used for session recreation scenarios

4. **Update `get_client()` to check connection state**:
   ```python
   if chat_id in self._sessions:
       session = self._sessions[chat_id]
       # Check if client is still connected
       if session.client.state == ConnectionState.CONNECTED:
           session.last_activity = now
           return session.client
       else:
           # Client disconnected - recreate session
           logger.info(f"Client for chat_id {chat_id} is disconnected, recreating...")
           await self._remove_client_only(chat_id)  # Remove but preserve workspace
   ```

### Changes to `handlers/private.py`

Enhanced error handling to provide user-friendly messages for session/connection issues:
```python
is_session_error = any(keyword in str(e).lower() for keyword in [
    'session', 'connection', 'disconnected', 'timeout'
])

if is_session_error:
    error_text = (
        f"❌ Connection or session issue detected.\n\n"
        f"Details: {e}\n\n"
        f"Please send your message again to retry with a fresh session."
    )
```

## How It Works

```
User sends message on Telegram
    ↓
handle_private_message() is called
    ↓
pool.get_client(chat_id) is called
    ↓
[CHECK] session.client.state == ConnectionState.CONNECTED ?
    ↓
  ├─ Yes → Return existing client (normal path)
  └─ No  → Remove old client (preserve workspace), create new one
      ↓
    Send message to jaato server with fresh session
```

## Key Characteristics

- **Lazy detection**: Verification happens **only when a message arrives**
- **No proactive monitoring**: The client doesn't actively poll the connection state
- **No periodic health checks**: We rely on the `IPCRecoveryClient`'s internal state tracking
- **Single-point verification**: All connection validation happens at one central point (`get_client()`)

## Recovery Behavior

When jaato-server connection drops:
1. Next message from user triggers reconnection check
2. Disconnected client is detected
3. Old client is removed (workspace preserved)
4. New client is created with fresh session
5. Message is processed successfully

Users don't need to manually run `/reset` - the system recovers automatically on the next message.

## Testing

Added `tests/test_connection_recovery.py` with comprehensive tests:
- Test connected client is returned
- Test disconnected client triggers recreation
- Test workspace is preserved during reconnection
