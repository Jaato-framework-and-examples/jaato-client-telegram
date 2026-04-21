# Permission Approval UI - Implementation Guide

## Overview

The jaato-client-telegram bot now supports **interactive permission approval** for tool execution. When the jaato agent requests permission to execute a tool (e.g., reading a file, making an API call), the bot displays an inline keyboard with Approve/Deny buttons, allowing users to control what the agent can do.

## Architecture

### Components

1. **PermissionHandler** (`permissions.py`)
   - Manages pending permission state per chat
   - Formats permission requests for display
   - Creates inline keyboards with response options
   - Parses callback data from button clicks

2. **Callback Handler** (`handlers/callbacks.py`)
   - Processes inline keyboard button clicks
   - Parses callback data (`perm:request_id:option_key`)
   - Updates permission message to show user's decision
   - Logs permission decision (TODO: send to SDK)

3. **Response Renderer** (`renderer.py`)
   - Monitors event stream for `PermissionRequestedEvent`
   - Pauses streaming when permission is requested
   - Sends permission UI to user
   - Stores pending permission for callback handling

4. **Bot Integration** (`bot.py`)
   - Creates PermissionHandler instance
   - Registers callback router with dispatcher
   - Injects permission_handler into renderer and handlers

## Event Flow

```
1. User sends message to bot
   ↓
2. Bot forwards to jaato via SDK
   ↓
3. Agent needs permission to execute tool
   ↓
4. SDK sends PermissionRequestedEvent
   ↓
5. Renderer detects event, pauses streaming
   ↓
6. PermissionHandler creates UI (text + keyboard)
   ↓
7. Bot sends permission request message
   ↓
8. User clicks button (Approve/Deny)
   ↓
9. Callback handler receives click
   ↓
10. Updates message to show decision
   ↓
11. Sends PermissionResponseRequest to SDK
   ↓
12. Streaming resumes with agent response
```

## Permission Request UI

### Message Format

```
🔐 Permission Request

Tool: `file_read`

Parameters:
  • path: /etc/hosts

Reason:
  I need to read the hosts file to check your configuration.

⚠️ This will read a system file.

Choose an action below:
```

### Response Options

The agent can provide custom response options. Default options include:

| Key | Label | Action | Emoji |
|-----|-------|--------|-------|
| `yes` | Allow | Execute tool | ✅ |
| `no` | Deny | Skip tool | ❌ |
| `always_allow` | Always Allow | Whitelist tool | 🔄 |
| `always_deny` | Always Deny | Blacklist tool | 🚫 |

## Data Structures

### PermissionRequestedEvent (from SDK)

```python
{
    "type": "permission.requested",
    "request_id": "abc123",
    "tool_name": "file_read",
    "tool_args": {"path": "/etc/hosts"},
    "response_options": [
        {"key": "yes", "label": "Allow", "action": "yes"},
        {"key": "no", "label": "Deny", "action": "no"}
    ],
    "prompt_lines": [
        "I need to read the hosts file",
        "to check your configuration"
    ],
    "warnings": "This will read a system file",
    "warning_level": "warning"
}
```

### Callback Data Format

```
perm:request_id:option_key

Example: perm:abc123:yes
```

### PendingPermission (state)

```python
@dataclass
class PendingPermission:
    request_id: str
    tool_name: str
    tool_args: dict[str, Any]
    prompt_lines: list[str] | None
    warnings: str | None
    warning_level: str | None
    response_options: list[dict[str, str]]
    chat_id: int
    message_id: int  # Telegram message to edit
```

## Usage Example

### As a User

1. Send a message: "Read /etc/passwd and show me the first line"
2. Bot responds: "I need to read a file to help with that."
3. Permission request appears:
   ```
   🔐 Permission Request
   
   Tool: `file_read`
   
   Parameters:
     • path: /etc/passwd
   
   Reason:
     I need to read the password file to show you the first line.
   
   ⚠️ Warning: Reading sensitive system files
   
   [✅ Allow] [❌ Deny]
   ```
4. Click **✅ Allow**
5. Message updates: "✅ Decision: Allow - Sending response to jaato..."
6. Agent executes tool and responds with result

### As a Developer

#### Permission Handler Setup

```python
from jaato_client_telegram.permissions import PermissionHandler

# Create handler (done automatically in bot.py)
permission_handler = PermissionHandler()

# Inject into renderer
renderer = ResponseRenderer(
    max_message_length=4096,
    edit_throttle_ms=500,
    permission_handler=permission_handler,
)

# Register in dispatcher context
dp["permission_handler"] = permission_handler
```

#### Custom Response Options

The jaato agent can provide custom response options in the permission event:

```python
# In your agent code (jaato server side)
await request_permission(
    tool_name="dangerous_operation",
    tool_args={"target": "production"},
    response_options=[
        {"key": "yes", "label": "Proceed", "action": "yes"},
        {"key": "no", "label": "Cancel", "action": "no"},
        {"key": "sandbox", "label": "Use Sandbox", "action": "sandbox"},
    ],
    prompt_lines=["This is a dangerous operation"],
    warnings="This will affect production",
    warning_level="danger",
)
```

## Current Limitations

### Phase 1 Implementation

✅ **Implemented:**
- Permission request UI creation and display
- Inline keyboard with custom response options
- Callback query handling for button clicks
- Pending permission tracking per chat
- Integration with event streaming
- Message formatting with HTML/Markdown support

⏳ **Partial Implementation:**
- Permission response logged (TODO: send to SDK client)
- Requires session pool integration in callback handler

❌ **Not Implemented:**
- "Always allow/deny" preferences (requires persistence)
- Permission history/audit log
- Permission timeout (auto-reject after N minutes)
- Batch permissions (approve multiple tools at once)

### TODO for Full Functionality

1. **Send PermissionResponseRequest to SDK**
   - Currently only logs the decision
   - Need to access SDK client from callback handler
   - Solution: Pass session_pool to callback handler or use dispatcher context

2. **Resume Streaming After Permission**
   - Currently pauses streaming and breaks
   - Need to continue event stream after permission decision
   - Solution: Send permission response and wait for next event from SDK

## Testing

### Manual Testing (Requires Running jaato Server)

1. Start jaato server with permission-requesting tools enabled
2. Start telegram bot: `python -m jaato_client_telegram`
3. Send message that triggers permission request
4. Verify UI appears correctly
5. Click Approve/Deny buttons
6. Verify message updates
7. Check logs for permission decision

### Unit Testing

```python
import pytest
from jaato_client_telegram.permissions import PermissionHandler

def test_permission_ui_creation():
    handler = PermissionHandler()
    
    # Mock permission event
    event = MockPermissionRequestedEvent(
        request_id="test123",
        tool_name="file_read",
        tool_args={"path": "/test"},
        response_options=[...],
    )
    
    text, keyboard = handler.create_permission_ui(event, chat_id=123)
    
    assert "file_read" in text
    assert keyboard is not None

def test_callback_parsing():
    handler = PermissionHandler()
    
    result = handler.parse_callback_data("perm:abc123:yes")
    assert result == ("abc123", "yes")
    
    result = handler.parse_callback_data("invalid")
    assert result is None
```

## Configuration

Permission handling is automatically enabled when the bot starts. The behavior of the permission UI can be customized via configuration.

### Configuring Unsupported Actions

Some action types (e.g., `comment`, `edit`, `input`) require complex UI elements like text input fields or modal dialogs that Telegram's inline keyboard doesn't support. These actions are filtered out from the permission UI.

You can customize which action types are filtered via configuration:

#### In `jaato-client-telegram.yaml`:

```yaml
permissions:
  # Action types to filter from inline keyboard (comma or pipe separated)
  # These require complex UI (text input, modals) that Telegram doesn't support
  # Example: "comment,edit,idle,turn,all" or "comment|edit|idle|turn|all"
  unsupported_actions: "comment,edit,modify,custom,input"
```

#### Via Environment Variable:

```bash
export JAATO_PERMISSION_UNSUPPORTED_ACTIONS="comment|edit|idle|turn|all"
```

#### Supported Formats:

- **Comma-separated**: `"comment,edit,idle,turn,all"`
- **Pipe-separated**: `"comment|edit|idle|turn|all"`
- **Mixed with whitespace**: `"comment, edit , idle | turn , all"` (whitespace is trimmed)

#### Default Behavior:

If no configuration is provided, the following action types are filtered by default:

- `comment` - Requires free-form text input
- `edit` - Requires editing existing text
- `modify` - Requires modification interface
- `custom` - Requires custom input
- `input` - Requires text input field

#### Fallback Behavior:

If all response options from the agent are filtered out, the bot automatically provides default "Allow" and "Deny" buttons to ensure the user can still respond to the permission request.

### Environment Variables

None - permission handling is always active.

### Whitelist Interaction

Permission requests bypass the whitelist check - all users can respond to permission requests for their own sessions. This is by design since permission requests are tied to active sessions.

## Troubleshooting

### Permission UI Not Appearing

**Check:** Is the permission handler integrated in bot.py?
```bash
grep permission_handler src/jaato_client_telegram/bot.py
```

**Check:** Is the callback router registered?
```bash
grep callbacks_router src/jaato_client_telegram/bot.py
```

**Check:** Are permission events being received?
```bash
tail -f logs/jaato-client-telegram.log | grep -i permission
```

### Buttons Not Responding

**Check:** Callback data format
```python
# Should be: perm:request_id:option_key
# Example: perm:abc123:yes
```

**Check:** Is the pending permission stored?
```python
# In callback handler, add debug:
pending = permission_handler.get_pending(chat_id)
logger.info(f"Pending: {pending}")
```

### Compilation Errors

```bash
# Verify all files compile
python -m py_compile \
    src/jaato_client_telegram/permissions.py \
    src/jaato_client_telegram/handlers/callbacks.py \
    src/jaato_client_telegram/bot.py
```

## Future Enhancements

1. **Permission Preferences** - Remember user choices (e.g., "always allow file reads")
2. **Audit Log** - Track all permission requests and decisions
3. **Timeouts** - Auto-reject pending permissions after N minutes
4. **Batch Permissions** - Approve multiple tools at once
5. **Risk Levels** - Color-code permissions by risk (safe/moderate/dangerous)
6. **Admin Override** - Allow admins to approve permissions for other users
7. **Permission History** - Show user their permission history
8. **Tool Whitelisting** - Always allow certain tools (e.g., file_read in workspace)

## Related Documentation

- [WHITELIST.md](WHITELIST.md) - User access control
- [ACCESS_REQUEST_WORKFLOW.md](ACCESS_REQUEST_WORKFLOW.md) - New user access requests
- [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md) - Overall architecture
- [jaato-sdk events](../jaato/jaato-sdk/jaato_sdk/events.py) - Event definitions

## Support

For issues or questions:
1. Check logs: `tail -f logs/jaato-client-telegram.log`
2. Verify compilation: `python -m py_compile src/**/*.py`
3. Test with debug logging: `export LOG_LEVEL=DEBUG`
4. Open an issue with logs and error messages
