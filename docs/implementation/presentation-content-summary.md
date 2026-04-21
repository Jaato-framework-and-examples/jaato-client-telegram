# Implementation Summary: Presentation Context & Expandable Content

**Date:** 2025-02-18
**Features:** Agent Presentation Awareness + Expandable Tool Outputs
**Status:** ✅ Complete and Tested

---

## Overview

Two complementary features were implemented to dramatically improve the mobile Telegram experience when interacting with AI agents that produce wide or structured output (JSON, code, tables, logs).

### Problem Solved

**Before:**
- Agent had no awareness of mobile display constraints (45 chars width)
- Wide tool outputs (JSON, tables, code) overflowed and broke mobile layout
- Horizontal scrolling made content unreadable
- No way to collapse verbose tool outputs

**After:**
- Agent knows client capabilities via presentation context
- Agent can produce detailed output knowing client handles overflow
- Wide content automatically collapsed into expandable blockquotes
- Clean mobile UX with tap-to-expand interaction

---

## Feature 1: Expandable Content for Tool Outputs

### What It Does

Automatically detects wide tool output and wraps it in Telegram's `expandable_blockquote` entity, making it collapsed-by-default with tap-to-expand functionality.

### Detection Logic

Content is flagged as "wide" if it contains:
- Lines longer than 100 characters
- JSON objects (`{` and `}`)
- Code blocks (`` ``` `` or backticks)
- Table separators (`|`)
- URLs longer than 80 characters

### Implementation

**Files Modified:**
- `src/jaato_client_telegram/renderer.py`
  - Added `_is_wide_content()` method
  - Added `_format_expandable_blockquote()` method
  - Modified `stream_response()` to handle `source="tool"` events
  - Updated `_edit_or_send()` and `send_final_response()` to use HTML parse mode

**Example:**
```python
# Tool output: {"user": {"id": 123, "name": "Alice", ...}}
# Detected as: JSON + long lines
# Formatted as: <blockquote>||<json content>||</blockquote>
# User sees: [▼ Tool output] (collapsed)
# User taps: Expands to show full JSON
```

### Testing

- `tests/test_expandable_content.py` - 11 tests, all passing
- Tests cover JSON, long lines, code blocks, tables, URLs
- Tests verify formatting correctness and whitespace handling

---

## Feature 2: Presentation Context Declaration

### What It Does

Declares the Telegram client's display capabilities to the jaato server, allowing the AI agent to adapt its output format for mobile constraints.

### Presentation Context

The client declares:
```python
{
    "content_width": 45,              # Mobile viewport
    "supports_markdown": True,        # Basic formatting
    "supports_tables": False,         # Tables don't render well
    "supports_code_blocks": True,     # Code is OK
    "supports_images": True,          # Images work
    "supports_expandable_content": True,  # We handle overflow
    "client_type": "chat",            # Messaging platform
    # ... other capabilities
}
```

### How It Works

1. **Connection:** User sends first message to bot
2. **Session Creation:** `SessionPool.get_client()` creates SDK client
3. **Context Declaration:** `ClientConfigRequest` event sent with presentation dict
4. **Server Processing:** Server constructs `PresentationContext` from dict
5. **System Instructions:** Context injected into agent's system prompt:
   ```
   ## Display Context
   Output width: 45 characters.
   The client can collapse wide or long content behind an expandable control.
   You may use full-width tables and detailed output freely.
   ```
6. **Agent Adaptation:** Agent now knows it can produce detailed output

### Implementation

**Files Modified:**
- `src/jaato_client_telegram/session_pool.py`
  - Added `create_telegram_presentation_context()` function
  - Modified `get_client()` to send `ClientConfigRequest` after connection
  - Gracefully handles SDK versions without presentation support

**Backward Compatibility:**
- Wrapped in `try/except` to handle older SDK versions
- Presentation context is optional - bot works without it
- Fallback to regular rendering if SDK doesn't support it

### Testing

- `tests/test_presentation_context.py` - 15 tests, all passing
- Tests verify all fields, types, and JSON serialization
- Tests ensure compatibility with SDK event format

---

## Integration: How Features Work Together

### Scenario: Database Query Result

```
User: "Show me all users"

[Server receives presentation context]
→ Agent knows: width=45, expandable=True, tables=False
→ Agent decides: Produce detailed result, client will handle overflow

[Agent executes query]
→ Returns wide table with 10 columns

[Client receives tool output event]
→ source="tool", content=<wide table>
→ _is_wide_content() detects: long lines
→ _format_expandable_blockquote() wraps: <blockquote>||...||</blockquote>

[User sees in Telegram]
▼ Tool output: query_results (collapsed)
  [Tap to expand]

[User taps]
▼ Tool output: query_results (expanded)
  | ID | Name | Email | ... |
  |----|------|-------|-----|
  | 1  | Alice | ...   | ... |
  ...
```

### Responsibility Split

| Component | Responsibility |
|-----------|---------------|
| **Agent** | Decides *what* content to produce (based on presentation context) |
| **Client** | Decides *how* to present overflow (expandable blockquotes) |
| **User** | Decides *what* to expand (tap-to-interact) |

This clean separation means:
- Agent can be detailed without breaking UI
- Client handles presentation logic (not server)
- User controls their reading experience

---

## Benefits

### For Users
✅ No horizontal scrolling on mobile
✅ Clean chat view (verbose content collapsed)
✅ Tap-to-expand interaction (familiar pattern)
✅ Better readability of structured data

### For Agent
✅ Knows it can produce detailed output
✅ Doesn't need to truncate or summarize excessively
✅ Can include full JSON responses, code examples
✅ Focuses on content quality over width constraints

### For Developers
✅ Automatic (no manual formatting needed)
✅ Backward compatible (works with older SDK)
✅ Well-tested (26 tests passing)
✅ Documented (3 new documentation files)

---

## Documentation

### New Files Created

1. **`EXPANDABLE_CONTENT.md`**
   - Feature overview
   - Detection logic details
   - Implementation guide
   - Testing instructions

2. **`PRESENTATION_CONTEXT.md`**
   - Integration overview
   - Field explanations
   - Example scenarios
   - Benefits and testing

3. **`tests/test_expandable_content.py`**
   - 11 tests for wide content detection
   - Tests for formatting logic
   - Whitespace handling tests

4. **`tests/test_presentation_context.py`**
   - 15 tests for context structure
   - Field type validation
   - JSON serialization tests

### Updated Files

- **`README.md`** - Added two new features to feature list
- **`src/jaato_client_telegram/renderer.py`** - Expandable content implementation
- **`src/jaato_client_telegram/session_pool.py`** - Presentation context declaration

---

## Testing Results

```
✓ tests/test_expandable_content.py::TestExpandableContent - 11/11 passed
✓ tests/test_presentation_context.py::TestPresentationContext - 15/15 passed
✓ Syntax validation: All files compile without errors
```

---

## Future Enhancements

Potential improvements:
- Detect desktop vs mobile Telegram (different widths)
- Update context on screen rotation (if detectable)
- Add per-user preference for expandable behavior
- Syntax highlighting in expanded code blocks
- Copy/download buttons for expanded content
- Customize detection thresholds per user

---

## References

- [Agent Presentation Awareness Design](/home/apanoia/Sources/jaato/docs/design/agent-presentation-awareness.md)
- [Telegram Bot API - Message Entities](https://core.telegram.org/bots/api#messageentity)
- [Expandable Content Feature](./EXPANDABLE_CONTENT.md)
- [Presentation Context Integration](./PRESENTATION_CONTEXT.md)
