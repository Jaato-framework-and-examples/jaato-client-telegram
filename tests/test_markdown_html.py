"""Tests for markdown -> Telegram HTML conversion (markdown_to_telegram_html)."""

from jaato_client_telegram.renderer import markdown_to_telegram_html as md


def test_bold():
    assert md("hello **world** now") == "hello <b>world</b> now"


def test_italic():
    assert md("an *emphasised* word") == "an <i>emphasised</i> word"


def test_bold_and_italic_together():
    assert md("**bold** and *it*") == "<b>bold</b> and <i>it</i>"


def test_inline_code():
    assert md("call `do_thing()` now") == "call <code>do_thing()</code> now"


def test_code_block():
    out = md("see:\n```python\nx = 1\n```\ndone")
    assert "<pre>python\nx = 1</pre>" in out or "<pre>x = 1</pre>" in out
    assert "```" not in out


def test_link():
    assert md("see [docs](https://example.com/x)") == 'see <a href="https://example.com/x">docs</a>'


def test_strikethrough():
    assert md("~~gone~~") == "<s>gone</s>"


def test_markdown_inside_inline_code_is_literal():
    # The ** inside backticks must NOT become bold.
    out = md("`a **b** c`")
    assert out == "<code>a **b** c</code>"


def test_markdown_inside_fence_is_literal():
    out = md("```\nx = a **not bold** b\n```")
    assert "<b>" not in out
    assert "**not bold**" in out


def test_existing_pre_block_untouched():
    # Server-rendered HTML code (already escaped) must pass through unchanged,
    # even if it contains asterisks.
    src = "<pre><code>   1 | a = b * c ** d</code></pre>"
    assert md(src) == src


def test_snake_case_not_italicised():
    # Single underscores are intentionally NOT emphasis (would wreck identifiers).
    assert md("the file_path and STORE_PATH vars") == "the file_path and STORE_PATH vars"


def test_asterisk_bullets_not_italicised():
    # "* item" (space after *) is a list bullet, not emphasis.
    text = "* first\n* second"
    assert md(text) == text


def test_no_markup_is_noop():
    assert md("just plain text, nothing here") == "just plain text, nothing here"


def test_real_shopping_list_renders_bold_header():
    text = "🛒 **Shopping list (3 items):**\n  1. iogurt\n  2. lettuce\n  3. shampoo"
    out = md(text)
    assert "<b>Shopping list (3 items):</b>" in out
    assert "1. iogurt" in out and "3. shampoo" in out  # list text intact
    assert "**" not in out


def test_escaped_entities_preserved():
    # Input is already HTML-escaped; conversion must not corrupt entities.
    assert md("compare **&lt;tag&gt;** here") == "compare <b>&lt;tag&gt;</b> here"
