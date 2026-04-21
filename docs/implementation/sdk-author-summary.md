# Telegram Client Rendering Pipeline - Summary for SDK Author

## Quick Overview

The jaato-client-telegram implements the canonical buffering pattern from the jaato event protocol, with Telegram-specific adaptations for permission handling and message editing constraints.

---

## Core Implementation

### Event Processing Pipeline

```python
async for event in client.events():
    # 1. Model output with flush detection
    if event.type == "agent.output" and event.source == "model":
        if event.mode == "flush":
            # Text streaming done, tools about to start
            _flush_text_buffer(ctx)
            await _edit_or_send(message, ctx)
        elif event.mode in ("write", "append"):
            # Buffer text chunks
            ctx.text_buffer.append(event.text)
    
    # 2. Permission requests (immediate placement)
    elif event.type == "permission.input_mode":
        _flush_text_buffer(ctx)
        # Add placeholder NOW at correct position
        ctx.accumulated_text += f"\n\n▶️ Decision: pending\n\nTool: {event.tool_name}"
        ctx.permissions_added_to_text.add(event.request_id)
        await _edit_or_send(message, ctx)
        # Show interactive UI as separate message
        await _show_permission_ui(event)
    
    # 3. Turn completion
    elif event.type == "turn.completed":
        _flush_all_buffers(ctx)  # Skips already-added permissions
        await _edit_or_send(message, ctx)
        break
```

---

## Protocol Compliance

### ✅ Follows Canonical Buffering Pattern

From `jaato_event_protocol.md` Part 12:

```python
# Canonical pattern from protocol:
text_buffer = []
async for event in client.events():
    if isinstance(event, AgentOutputEvent) and event.source == "model":
        if event.mode == "flush":
            send_message("\n".join(text_buffer))
            text_buffer.clear()
        elif event.mode in ("write", "append"):
            text_buffer.append(event.text)
```

**Our implementation:**
- Same flush detection logic ✅
- Same buffering strategy ✅
- Added permission placeholder management (Telegram-specific requirement)

### ✅ Correct Flush Signal Handling

- `mode="flush"` detected as text-to-tool transition ✅
- Text displayed before tool execution ✅
- Multiple flush cycles supported ✅
- Text-only responses flush on `TurnCompletedEvent` ✅

---

## Key Design Decisions

### 1. Immediate Permission Placement

**Why:**
- Permissions must appear at specific position in message
- Telegram can't edit arbitrary sections, only entire message
- If we wait until turn completion, permissions appear at end

**How:**
```python
# On permission.input_mode:
ctx.accumulated_text += f"\n\n▶️ Decision: pending\n\nTool: {tool_name}"
ctx.permissions_added_to_text.add(request_id)  # Track to avoid duplication
```

**Result:**
- Permission placeholder appears in correct position during streaming
- Stays in correct position after turn completion
- Not duplicated in final flush

### 2. Separate Message for Interactive UI

**Why:**
- Telegram inline keyboards require `reply_markup` on a message
- Can't add keyboard to existing message being edited

**How:**
```python
perm_message = await message.answer(text, reply_markup=keyboard)
```

**Result:**
- Main message shows permission placeholder
- Separate message provides interactive buttons
- User experience is clean and intuitive

### 3. Tracking Set for Permissions

**Why:**
- Prevents adding permissions twice
- Allows selective flush (skip already-added permissions)

**How:**
```python
permissions_added_to_text: set[str] = field(default_factory=set)

# In _flush_tool_call_buffer:
if event.request_id in ctx.permissions_added_to_text:
    continue  # Skip, already in correct position
```

---

## Event Sequence Example

### Input: "Check Docker status"

```
1. AgentOutput(mode="write", "I'll check...")
   → Buffer: ["I'll check..."]

2. AgentOutput(mode="flush", "")
   → accumulated_text: "I'll check..."
   → Message edited ✅

3. PermissionInputMode(tool="cli_based_tool")
   → accumulated_text: "I'll check...\n\n▶️ Decision: pending\n\nTool: cli_based_tool"
   → permissions_added_to_text: {"perm-001"}
   → Message edited ✅
   → Separate UI message sent ✅

4. User clicks "Yes"

5. Tool executes → produces output

6. AgentOutput(mode="write", "Here's the status...")
   → Buffer: ["Here's the status..."]

7. AgentOutput(mode="flush", "")
   → accumulated_text: "I'll check...\n\n▶️ Decision: pending\n\nTool: cli_based_tool\n\nHere's the status..."
   → Message edited ✅

8. TurnCompleted()
   → _flush_all_buffers()
   → Skip "perm-001" (already added) ✅
   → Final message complete ✅
```

**Final Result:**
```
I'll check the status of your Docker containers for you.

▶️ Decision: yes
Tool: cli_based_tool

Here's the status of your Docker containers:
[... output ...]
```

Permission appears in correct position ✅

---

## Testing

### Test Coverage

All tests pass ✅

1. **`test_flush_mode_triggers_text_display`**
   - Validates flush detection and text display

2. **`test_text_only_response_no_flush`**
   - Validates text-only responses (no flush signal)

3. **`test_multiple_flush_cycles`**
   - Validates multiple text→flush→tools cycles

4. **`test_buffers_cleared_on_flush`**
   - Validates buffer clearing on flush

---

## Questions for Verification

### 1. Flush Signal Semantics
**Q:** Is `mode="flush"` ALWAYS emitted immediately before tool execution starts?

**A:** We assume yes. Our implementation flushes text on `mode="flush"` and expects tools to follow.

### 2. Permission Event Order
**Q:** Is `permission.input_mode` the definitive event to trigger permission UI display?

**A:** We assume yes. We add the placeholder on this event and show the interactive UI.

### 3. Text-Only Responses
**Q:** Is it correct that text-only responses skip `mode="flush"` and go directly to `TurnCompletedEvent`?

**A:** We assume yes. We flush remaining text buffer on `TurnCompletedEvent` as fallback.

### 4. Multiple Permissions
**Q:** Can multiple permissions be pending simultaneously in a single turn?

**A:** Our implementation supports this via the `permissions_added_to_text` tracking set.

---

## Edge Cases Handled

✅ Text-only responses (no tool calls)  
✅ Multiple flush cycles per turn  
✅ Multiple permissions in one turn  
✅ Long messages (>4096 chars)  
✅ Permission placeholders preserved after answering  
✅ No duplication of permission prompts  

---

## Documentation

Full details available in:
- `RENDERING_PIPELINE.md` - Complete implementation guide
- `EVENT_FLOW_DIAGRAM.md` - Visual event flow diagrams

---

## Conclusion

The rendering pipeline:
- ✅ Follows SDK event protocol correctly
- ✅ Implements canonical buffering pattern with flush detection
- ✅ Handles Telegram's messaging constraints appropriately
- ✅ Preserves event ordering (text → permissions → tools → text)
- ✅ Tested and working

**Ready for review by SDK author.**
