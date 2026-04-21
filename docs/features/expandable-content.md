# Expandable Content for Tool Outputs

## Overview

The Telegram bot client now automatically detects and formats wide tool outputs (JSON, code, tables, long lines) as **expandable blockquotes**. This prevents horizontal scrolling and improves readability on mobile devices.

## How It Works

When the renderer receives `agent.output` events with `source="tool"`, it analyzes the content:

1. **Wide Content Detection**: Content is flagged as "wide" if it contains:
   - Lines longer than 100 characters
   - JSON objects (`{` and `}`)
   - Code blocks (`` ``` `` or backticks)
   - Table separators (`|`)
   - URLs longer than 80 characters

2. **Automatic Formatting**: Wide content is wrapped in Telegram's `expandable_blockquote` entity using HTML syntax:
   ```html
   <blockquote>||content here||</blockquote>
   ```

3. **User Experience**: The content appears **collapsed by default** (~3 lines visible). Users tap to expand and see the full output.

## Examples

### JSON Output
```
Tool returned user data:
||{"user": {"id": 123, "name": "Alice", "email": "alice@example.com", ...}}||
```
Collapsed: Shows first few lines
Expanded: Full JSON structure

### Code Block
```
Executed function:
||```python
def process(data):
    result = []
    for item in data:
        result.append(transform(item))
    return result
```||```

### Table Data
```
Query results:
||| Name | Age | City |
|------|-----|------|
| Alice | 30 | NYC |
| Bob | 25 | LA |```

## Implementation Details

### Detection Logic
```python
def _is_wide_content(self, text: str) -> bool:
    # Check for long lines, JSON, code, tables, URLs
    # Returns True if content should be expandable
```

### Formatting
```python
def _format_expandable_blockquote(self, content: str) -> str:
    # Wraps content in <blockquote>||...||</blockquote>
    # Cleans trailing whitespace
    # Preserves internal structure
```

### Integration
```python
elif source == "tool":
    if self._is_wide_content(content):
        expandable = self._format_expandable_blockquote(content)
        ctx.text_buffer.append(expandable)
    else:
        ctx.text_buffer.append(content)
```

## HTML Parse Mode

Messages containing expandable blockquotes are sent with `parse_mode="HTML"`:

```python
await message.answer(text, parse_mode="HTML")
```

The renderer automatically detects HTML content and applies the correct parse mode.

## Benefits

1. **Better Mobile Experience**: No horizontal scrolling for wide content
2. **Cleaner Chat View**: Long outputs are collapsed by default
3. **User Control**: Users choose when to expand and view full output
4. **Automatic**: No manual formatting needed - works out of the box

## Testing

Run the test suite:

```bash
pytest tests/test_expandable_content.py -v
```

Tests cover:
- JSON detection
- Long line detection
- Code block detection
- Table detection
- URL detection
- Formatting correctness
- Whitespace handling

## Future Enhancements

Potential improvements:
- Configurable width threshold
- Custom formatting per tool type
- Syntax highlighting for code blocks
- Copy button for code/JSON
- Download button for large outputs

## References

- [Telegram Bot API - Message Entities](https://core.telegram.org/bots/api#messageentity)
- [Telegram Formatting Options](https://gist.github.com/AmirOfficiaI/d2293ae0203043f851b00604784a2afc)
- [HTML Parse Mode](https://core.telegram.org/bots/api#html-style)
