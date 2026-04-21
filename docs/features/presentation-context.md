# Presentation Context Integration

## Overview

The Telegram bot client now declares its **presentation capabilities** to the jaato server using the SDK's new `PresentationContext` system. This allows the AI agent to adapt its output format based on the client's display constraints and capabilities.

## What This Enables

### Before (No Context)
The agent had no awareness of:
- Mobile display width (45 characters)
- Limited markdown support (no tables)
- Need for compact formatting

Result: Wide tables, long code blocks, and JSON outputs overflowed and broke the mobile layout.

### After (With Presentation Context)
The agent now knows:
- **Display width:** 45 characters (mobile Telegram)
- **Table support:** Disabled (tables don't render well)
- **Expandable content:** Enabled (we use expandable blockquotes)
- **Client type:** Chat platform

Result: The agent adapts its output:
- Uses vertical lists instead of wide tables
- Keeps code blocks concise
- Produces content that works well with our expandable blockquote feature
- Optimizes for mobile reading experience

## How It Works

### 1. Client Declaration

When a Telegram user connects, the session pool creates a presentation context:

```python
def create_telegram_presentation_context() -> dict:
    return {
        "content_width": 45,  # Mobile width
        "supports_markdown": True,  # Basic formatting
        "supports_tables": False,  # Tables don't work
        "supports_code_blocks": True,  # Code is OK
        "supports_images": True,  # Images supported
        "supports_expandable_content": True,  # We handle overflow
        "client_type": "chat",  # Messaging platform
        # ... other capabilities
    }
```

### 2. Server Communication

After connecting, the client sends a `ClientConfigRequest` event:

```python
from jaato_sdk.events import ClientConfigRequest

presentation_ctx = create_telegram_presentation_context()
config_event = ClientConfigRequest(presentation=presentation_ctx)
await client.send_event(config_event)
```

### 3. System Instruction Injection

The server injects presentation context into the agent's system instructions:

```
## Display Context
Output width: 45 characters.
The client can collapse wide or long content behind an expandable control.
You may use full-width tables and detailed output freely.
```

### 4. Agent Adaptation

The agent now knows it can:
- Use wider tables (we'll handle overflow with expandable blockquotes)
- Include detailed JSON/code (we'll collapse it)
- Focus on content quality over width constraints

But should avoid:
- Markdown tables (they don't render)
- Extremely long lines (we'll collapse, but shorter is better)
- Complex nested structures (hard to read even when expanded)

## Presentation Context Fields

| Field | Value | Rationale |
|-------|-------|-----------|
| `content_width` | 45 | Mobile Telegram viewport width |
| `supports_markdown` | True | Bold, italic, code, links work |
| `supports_tables` | False | Tables don't render well on mobile |
| `supports_code_blocks` | True | Inline code and blocks work |
| `supports_images` | True | Images display inline |
| `supports_rich_text` | True | Bold, italic, underline, strikethrough |
| `supports_unicode` | True | Full emoji and Unicode support |
| `supports_mermaid` | False | No diagram support |
| `supports_expandable_content` | **True** | We handle overflow with blockquotes |
| `client_type` | `"chat"` | Messaging platform |

## Expandable Content Strategy

The `supports_expandable_content: True` flag is key. It tells the agent:

> "You can produce wide/detailed output freely. The client will detect overflow and wrap it in an expandable widget."

This splits responsibilities cleanly:
- **Agent** decides what content to produce
- **Client** decides how to present overflow

Our implementation:
1. Agent produces tool output (JSON, code, tables, logs)
2. Renderer detects wide content with `_is_wide_content()`
3. Wide content is wrapped in `<blockquote>||...||</blockquote>`
4. Telegram renders it as collapsed-by-default, expandable on tap

## Example Scenarios

### Scenario 1: Database Query Result

**Agent's perspective:**
- Knows client supports expandable content
- Produces full table with all columns
- Doesn't worry about width

**Client's handling:**
```python
# Agent outputs: wide table with 10 columns
# Renderer detects: wide content (100+ char lines)
# Formats as: <blockquote>||<wide table>||</blockquote>
# User sees: [▼ Tool output ] (collapsed)
# User taps: Expands to see full table
```

### Scenario 2: JSON API Response

**Agent's perspective:**
- Knows client supports expandable content
- Returns full JSON response with all fields
- Doesn't need to truncate or summarize

**Client's handling:**
```python
# Agent outputs: {"user": {...}, "metadata": {...}, ...}
# Renderer detects: JSON (contains { and })
# Formats as: <blockquote>||<json>||</blockquote>
# User sees: [▼ API response ] (collapsed)
# User taps: Expands to see full JSON
```

### Scenario 3: Code Snippet

**Agent's perspective:**
- Knows client supports code blocks
- Provides working code example
- Can include explanatory comments

**Client's handling:**
```python
# Agent outputs: ```python\ndef process():\n    ...\n```
# Renderer detects: code block (contains ```)
# Formats as: <blockquote>||<code>||</blockquote>
# User sees: [▼ Code example ] (collapsed)
# User taps: Expands to see full code
```

## Benefits

1. **Better UX:** Agent produces optimal content; client handles presentation
2. **No Layout Breakage:** Wide content is collapsed, not overflowing
3. **User Control:** Tap to expand what they need, ignore what they don't
4. **Agent Awareness:** Model knows it can be detailed without breaking the UI
5. **Future-Proof:** As Telegram features evolve, we update the context

## Testing

To verify presentation context is working:

1. Check server logs for `ClientConfigRequest` events
2. Verify agent system instructions include display context
3. Test with wide tool outputs (JSON, tables, code)
4. Confirm expandable blockquotes appear in Telegram

```bash
# In server logs, look for:
# ClientConfigRequest(presentation={...})
# PresentationContext(content_width=45, supports_expandable_content=True, ...)
```

## Future Enhancements

Potential improvements:
- Detect desktop vs mobile Telegram (different widths)
- Update context on screen rotation (if detectable)
- Add support for `terminal_width` updates during session
- Customize context per-user preference
- Add `supports_syntax_highlighting` flag

## References

- [Agent Presentation Awareness Design](/home/apanoia/Sources/jaato/docs/design/agent-presentation-awareness.md)
- [Expandable Content Feature](./EXPANDABLE_CONTENT.md)
- [Telegram Bot API - Message Entities](https://core.telegram.org/bots/api#messageentity)
