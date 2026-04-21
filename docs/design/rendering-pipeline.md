# jaato-client-telegram Rendering Pipeline

## Executive Summary

This document describes how the Telegram client processes and renders events from the jaato SDK event protocol. The implementation follows the canonical buffering pattern described in the jaato event protocol documentation, with specific adaptations for Telegram's messaging constraints.

---

## Architecture Overview

### Event Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                     EVENT STREAM FLOW                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  jaato Server                                                    │
│       │                                                          │
│       │ IPC (Unix Socket)                                        │
│       ▼                                                          │
│  jaato_sdk (IPCRecoveryClient)                                   │
│       │                                                          │
│       │ async for event in client.events()                       │
│       ▼                                                          │
│  ResponseRenderer.stream_response()                              │
│       │                                                          │
│       ├──► Buffer model text chunks (text_buffer)                │
│       ├──► Detect mode="flush" → flush text, display            │
│       ├──► Buffer tool events (tool_call_buffer)                 │
│       ├──► Handle permission requests (inline position)          │
│       └──► On turn complete → finalize all buffers               │
│                                                                  │
│       ▼                                                          │
│  Telegram Bot API                                                │
│       ├──► Edit-in-place streaming (during turn)                │
│       ├──► Separate messages for permissions (interactive)       │
│       └──► Final message with complete response                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data Structures

### StreamingContext

```python
@dataclass
class StreamingContext:
    """State for edit-in-place streaming of responses."""
    
    sent_message: Message | None = None           # Telegram message being edited
    accumulated_text: str = ""                    # Final text to display
    last_edit_time: float = 0.0                   # Rate limiting
    edits_count: int = 0                          # Edit tracking
    seen_model_output: bool = False               # Formatting state
    
    # Buffers for ordering
    text_buffer: list[str] = field(default_factory=list)
    tool_call_buffer: list[dict] = field(default_factory=list)
    
    # Permission tracking
    permissions_added_to_text: set[str] = field(default_factory=set)
```

**Purpose:** Maintains streaming state across multiple events within a single turn.

---

## Event Processing Logic

### 1. Model Output Events (`AgentOutputEvent`)

**Event Structure:**
```python
{
    "type": "agent.output",
    "source": "model",          # vs "tool", "system", plugin_name
    "mode": "write" | "append" | "flush",
    "text": "content chunk"
}
```

**Processing Rules:**

```python
if event.source == "model":
    if event.mode == "flush":
        # ⭐ CRITICAL: Model text streaming is DONE
        # Flush text buffer and display immediately
        _flush_text_buffer(ctx)
        await _edit_or_send(message, ctx)
        
    elif event.mode in ("write", "append"):
        # Buffer text chunks for later display
        ctx.text_buffer.append(event.text)
```

**Key Points:**
- `mode="flush"` is the synchronization point between text streaming and tool execution
- Text is **buffered** (not displayed) until flush arrives
- This ensures text appears before tool calls, not interleaved

---

### 2. Permission Request Flow

**Event Sequence:**
```
permission.requested → permission.input_mode → user responds → permission.resolved
```

**Processing Strategy:**

```python
# On permission.input_mode:
if event.type == "permission.input_mode":
    # 1. Flush any pending text FIRST
    _flush_text_buffer(ctx)
    
    # 2. Add permission placeholder to accumulated_text NOW
    request_id = event.request_id
    tool_name = event.tool_name
    placeholder = f"\n\n▶️ Decision: pending\n\nTool: {tool_name}"
    ctx.accumulated_text += placeholder
    
    # 3. Track that we added this permission
    ctx.permissions_added_to_text.add(request_id)
    
    # 4. Update message to show permission in correct position
    await _edit_or_send(message, ctx)
    
    # 5. Show interactive UI as SEPARATE message
    text, keyboard = permission_handler.create_permission_ui(event)
    perm_message = await message.answer(text, reply_markup=keyboard)
```

**Why This Approach:**

1. **Placeholder added immediately** - Ensures permission stays in correct position
2. **Interactive UI as separate message** - Telegram inline keyboard requires separate message
3. **Tracking prevents duplication** - Permission won't be added again during final flush

**After User Answers:**

```python
# On permission.resolved:
if event.type == "permission.resolved":
    # Update the placeholder to show the decision
    # (Placeholder already in accumulated_text at correct position)
    # Could edit the message to update "pending" → "yes"/"no"/"all"
```

---

### 3. Tool Call Events

**Event Types:**
- `tool.call.start` - Tool execution begins
- `tool.call.end` - Tool execution completes

**Processing:**

```python
# Buffer tool events for later display
ctx.tool_call_buffer.append(event)
```

**Display Logic:**

Tool calls are added to text via `_flush_tool_call_buffer()`:
- Non-permission tools: Added at flush points or turn completion
- Permission tools: Skipped (already added during streaming)

---

### 4. Turn Completion (`TurnCompletedEvent`)

**Finalization Steps:**

```python
if event.type == "turn.completed":
    # 1. Flush all remaining buffers
    _flush_all_buffers(ctx)
    
    # 2. Check for formatted_text
    if event.formatted_text:
        # Server provided full response
        # Delete streaming message, send final
        await ctx.sent_message.delete()
        await send_final_response(message, ctx)
    else:
        # No formatted_text, use accumulated streaming text
        await _edit_or_send(message, ctx)
    
    break  # Exit streaming loop
```

**Buffer Flush Order:**

```python
def _flush_all_buffers(ctx):
    # 1. Text first (model output)
    _flush_text_buffer(ctx)
    
    # 2. Tool calls (non-permission only)
    _flush_tool_call_buffer(ctx)
```

---

## Complete Event Sequence Example

### Scenario: User asks "Check Docker status"

```
1. AgentOutputEvent(source="model", mode="write", text="I'll check...")
   → Buffer: ["I'll check..."]

2. AgentOutputEvent(source="model", mode="append", text=" the status...")
   → Buffer: ["I'll check...", " the status..."]

3. AgentOutputEvent(source="model", mode="flush", text="")
   → FLUSH → Display: "I'll check the status..."
   → Clear buffer

4. PermissionRequestedEvent(tool_name="cli_based_tool")
   → Add to tool_call_buffer

5. PermissionInputModeEvent(request_id="perm-001")
   → Flush text (already done)
   → Add placeholder: "\n\n▶️ Decision: pending\n\nTool: cli_based_tool"
   → Update message: "I'll check the status...\n\n▶️ Decision: pending\n\nTool: cli_based_tool"
   → Track: permissions_added_to_text.add("perm-001")
   → Show inline keyboard as separate message

6. User clicks "yes"

7. ToolCallStartEvent(tool_name="cli_based_tool")
8. ToolCallEndEvent(tool_name="cli_based_tool", success=true)
   → Add to tool_call_buffer

9. AgentOutputEvent(source="model", mode="write", text="Here's the status...")
   → Buffer: ["Here's the status..."]

10. AgentOutputEvent(source="model", mode="flush", text="")
    → FLUSH → Display: "I'll check...\n\n▶️ Decision: pending\n\nTool: cli_based_tool\n\nHere's the status..."

11. [More tool output, more text chunks...]

12. TurnCompletedEvent()
    → _flush_all_buffers()
    → _flush_text_buffer(): Adds remaining text
    → _flush_tool_call_buffer(): 
        - Skips "perm-001" (already in permissions_added_to_text)
        - Adds any non-permission tools
    → Final message with complete response
```

---

## Key Design Decisions

### 1. Why Buffer Text Instead of Displaying Immediately?

**Reason:** Telegram messages can't be edited at arbitrary positions. We need to compose the complete message structure before displaying.

**Benefit:** Ensures permissions appear at the correct position relative to text.

### 2. Why Add Permission Placeholders Immediately?

**Reason:** If we wait until turn completion to add permissions, they'll appear at the end.

**Benefit:** Permission placeholders stay in the correct position throughout the turn.

### 3. Why Track `permissions_added_to_text`?

**Reason:** To avoid adding permissions twice (once during streaming, once during final flush).

**Benefit:** Clean output without duplication, permissions stay in original position.

### 4. Why Separate Message for Interactive UI?

**Reason:** Telegram inline keyboards require a separate message with `reply_markup`.

**Benefit:** Interactive buttons work natively, permission state tracked separately.

---

## Alignment with SDK Protocol

### Compliance with jaato_event_protocol.md

✅ **Part 12: Client Implementation Guide (Output Buffering)**

Our implementation follows the canonical buffering pattern:

```python
# From protocol:
text_buffer = []
async for event in client.events():
    if isinstance(event, AgentOutputEvent) and event.source == "model":
        if event.mode == "flush":
            send_message("\n".join(text_buffer))
            text_buffer.clear()
        elif event.mode in ("write", "append"):
            text_buffer.append(event.text)
```

**Our adaptation:** 
- Same flush detection logic ✅
- Added permission placeholder management (Telegram-specific)
- Added tool call buffering (Telegram-specific)

✅ **Flush Signal Handling**

- Detect `mode="flush"` to know when model text ends ✅
- Flush on `TurnCompletedEvent` for text-only responses ✅
- Multiple flush cycles per turn supported ✅

✅ **Source Filtering**

- Buffer `source="model"` only ✅
- Ignore `source="user"` (echo) ✅
- Handle `source="tool"`, `source="system"` separately ✅

---

## Testing Coverage

### Test Cases

1. **`test_flush_mode_triggers_text_display`**
   - Validates: `mode="flush"` triggers text display before tool execution

2. **`test_text_only_response_no_flush`**
   - Validates: Text-only responses work without `flush` signal

3. **`test_multiple_flush_cycles`**
   - Validates: Multiple text→flush→tools cycles in one turn

4. **`test_buffers_cleared_on_flush`**
   - Validates: Buffers properly cleared on flush signal

All tests pass ✅

---

## Edge Cases Handled

### 1. Text-Only Responses (No Tool Calls)
- No `flush` signal emitted
- `TurnCompletedEvent` triggers final flush
- Works correctly ✅

### 2. Multiple Flush Cycles
- Model may output text, flush, tools, then more text
- Each flush correctly positions text before following tools
- Works correctly ✅

### 3. Multiple Permissions in One Turn
- Each permission tracked by `request_id`
- Each added at correct position
- No duplication ✅

### 4. Long Messages (>4096 chars)
- Split at paragraph boundaries
- Original streaming message deleted
- Multiple messages sent sequentially
- Works correctly ✅

---

## Performance Characteristics

### Memory Usage

- `text_buffer`: O(number of chunks) - cleared on each flush
- `tool_call_buffer`: O(tool events per turn) - cleared on completion
- `permissions_added_to_text`: O(permissions per turn) - negligible

### Rate Limiting

- Edit throttling: 500ms minimum between edits
- Reduces Telegram API pressure
- Configurable via `edit_throttle_ms`

### Message Updates

- During streaming: Edit-in-place (1 message)
- Permissions: 1 additional message per permission
- Final: Either edit or delete + resend (if too long)

---

## Future Enhancements

### Potential Improvements

1. **Edit permission placeholder after user answers**
   - Update "pending" → "yes" in the main message
   - Requires message editing after user responds

2. **Show tool progress during execution**
   - Display running tools with spinner
   - Update on `ToolCallEndEvent`

3. **Better formatting for tool results**
   - Show tool output inline if useful
   - Collapsible sections for verbose output

---

## Questions for SDK Author

### Verification Points

1. **Flush Signal Semantics**
   - Q: Is `mode="flush"` ALWAYS emitted before tool execution?
   - Q: Can there be text after tools without another flush?
   - A: Our implementation assumes flush = "text done, tools next"

2. **Permission Event Order**
   - Q: Is `permission.input_mode` always the last permission-related event before tool execution?
   - Q: Can there be multiple permissions pending simultaneously?
   - A: Our implementation adds placeholder on `permission.input_mode`

3. **Text-Only Response Detection**
   - Q: Is it correct that text-only responses skip `flush` and go to `TurnCompletedEvent`?
   - A: Our implementation flushes on `TurnCompletedEvent` as fallback

4. **Multiple Flush Cycles**
   - Q: Can a turn have multiple text→flush→tools→text→flush→tools cycles?
   - A: Our implementation supports this by resetting buffer on each flush

---

## Conclusion

The rendering pipeline:
- ✅ Follows SDK event protocol correctly
- ✅ Implements canonical buffering pattern
- ✅ Handles Telegram's constraints appropriately
- ✅ Preserves event ordering (text → permissions → tools → text)
- ✅ Tested and working

**Key Innovation:** Immediate placement of permission placeholders ensures they stay in correct position throughout the turn, solving the repositioning bug that occurred when flushing everything at the end.
