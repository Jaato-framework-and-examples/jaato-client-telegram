# Connection Recovery Fix

## Problem

The SessionPool was implementing an incorrect connection recovery pattern. When the jaato-server connection dropped and then recovered, the bot would:

1. Detect that the client was disconnected
2. Remove the old client from the pool
3. Create a completely new client with a **new session_id**
4. **Lose all conversation state** from the previous session

This happened even though `IPCRecoveryClient` has built-in automatic reconnection capabilities.

## Root Cause

Two critical issues:

1. **Missing `set_session_id()` call** - After creating a session with `create_session()`, the code never called `set_session_id()` to tell IPCRecoveryClient which session to reattach to after reconnection.

2. **Recreating clients on disconnection** - When `client.state != ConnectionState.CONNECTED`, the code would remove and recreate the client, creating a new session and losing state.

## Solution

Following the [jaato connection recovery guide](https://jaato-framework-and-examples.github.io/jaato/web/guides/connection-recovery.html), the fix implements the correct pattern:

### 1. Call `set_session_id()` After Creating a Session

```python
# Create a dedicated session for this user
session_id = await client.create_session(name=f"telegram-{chat_id}")

# CRITICAL: Call set_session_id() so IPCRecoveryClient can
# reattach to this session after connection drops
client.set_session_id(session_id)
```

This stores the session ID in the IPCRecoveryClient instance. When the connection drops and reconnects, the client will automatically send a `session.attach` command with this session_id.

### 2. Return the Same Client Even When Disconnected

```python
# Check if client already exists for this chat_id
if chat_id in self._sessions:
    session = self._sessions[chat_id]
    
    # Update last activity timestamp
    session.last_activity = now
    
    # Return the client even if it's disconnected or reconnecting
    # IPCRecoveryClient will handle automatic reconnection
    # We do NOT recreate the client to preserve session state
    return session.client
```

The key insight is that we should **not** recreate clients when they're disconnected. IPCRecoveryClient has built-in recovery logic:

- Uses exponential backoff to retry connections
- Automatically transitions between states: CONNECTED → RECONNECTING → CONNECTED
- After reconnecting, sends `session.attach` with the stored session_id
- The server restores session state from disk (if evicted from memory)

## How It Works Now

When the jaato-server connection drops:

1. **Client detects disconnection** - IPCRecoveryClient state changes to `RECONNECTING`
2. **Automatic reconnection** - IPCRecoveryClient retries connection with exponential backoff
3. **Session reattachment** - After connecting, sends `session.attach` with the stored session_id
4. **State restoration** - Server loads session from disk and restores conversation history
5. **Continue operation** - Events stream resumes as if nothing happened

**What's preserved:**
- ✅ Session ID
- ✅ Conversation history
- ✅ Tool states
- ✅ All session state managed by the server

**What's lost:**
- ❌ Active IPC connection (replaced by new one)
- ❌ In-flight requests (pending permission responses)
- ❌ Real-time event stream (restarted after reconnect)

## Code Changes

### SessionPool (`src/jaato_client_telegram/session_pool.py`)

1. **Removed** `_remove_client_only()` method - no longer needed
2. **Updated** class docstring with recovery pattern documentation
3. **Updated** `get_client()` docstring explaining the correct pattern
4. **Modified** `get_client()` to:
   - Add `client.set_session_id(session_id)` call
   - Return same client when disconnected instead of recreating
   - Let IPCRecoveryClient handle reconnection automatically

### Tests (`tests/test_connection_recovery.py`)

1. **Rewrote** all tests to verify correct recovery pattern
2. **Added** `test_get_client_calls_set_session_id()` - Verifies set_session_id is called
3. **Added** `test_get_client_returns_same_client_when_disconnected()` - Verifies client is not recreated
4. **Added** `test_get_client_preserves_session_across_reconnection_states()` - Verifies behavior across all states
5. **Added** `test_different_chat_ids_create_different_clients()` - Verifies isolation still works

## Verification

All connection recovery tests pass:

```bash
$ python -m pytest tests/test_connection_recovery.py -v
...
tests/test_connection_recovery.py::TestConnectionRecovery::test_get_client_calls_set_session_id PASSED
tests/test_connection_recovery.py::TestConnectionRecovery::test_get_client_returns_same_client_when_disconnected PASSED
tests/test_connection_recovery.py::TestConnectionRecovery::test_get_client_preserves_session_across_reconnection_states PASSED
tests/test_connection_recovery.py::TestConnectionRecovery::test_different_chat_ids_create_different_clients PASSED

============================== 4 passed ===============================
```

## References

- [Connection Recovery Guide](https://jaato-framework-and-examples.github.io/jaato/web/guides/connection-recovery.html)
- [IPCRecoveryClient API](https://jaato-framework-and-examples.github.io/jaato/api-reference/ipc-recovery-client.html)
- [jaato-sdk Source Code](https://github.com/Jaato-framework-and-examples/jaato-sdk)

## Key Takeaways

1. **Always call `set_session_id()` after `create_session()`** - This is required for session reattachment
2. **Never recreate clients on disconnection** - Let IPCRecoveryClient handle recovery automatically
3. **Trust the SDK's built-in resilience** - The SDK has sophisticated retry logic and backoff strategies
4. **Session state lives on the server** - Client is just a conduit; server manages persistence
5. **Test connection recovery** - Verify that sessions survive connection drops and reconnect with same ID
