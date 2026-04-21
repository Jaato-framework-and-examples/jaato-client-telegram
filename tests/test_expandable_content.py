"""Tests for expandable content functionality in renderer."""

import pytest
from jaato_client_telegram.renderer import ResponseRenderer


class TestExpandableContent:
    """Test expandable blockquote functionality for wide tool outputs."""

    def test_is_wide_content_json(self):
        """Test that JSON content is detected as wide."""
        renderer = ResponseRenderer()

        json_content = '{"user": {"name": "Alice", "age": 30, "address": {"street": "123 Main St", "city": "Springfield"}}}'
        assert renderer._is_wide_content(json_content) is True

    def test_is_wide_content_long_line(self):
        """Test that very long lines are detected as wide."""
        renderer = ResponseRenderer()

        long_line = "This is a very long line that exceeds one hundred characters and should be detected as wide content that needs to be in an expandable blockquote"
        assert renderer._is_wide_content(long_line) is True

    def test_is_wide_content_code_block(self):
        """Test that code blocks are detected as wide."""
        renderer = ResponseRenderer()

        code_block = """```
def hello():
    print("world")
```"""
        assert renderer._is_wide_content(code_block) is True

    def test_is_wide_content_inline_code(self):
        """Test that inline code is detected as wide."""
        renderer = ResponseRenderer()

        inline_code = "Use `print('hello')` to output text"
        assert renderer._is_wide_content(inline_code) is True

    def test_is_wide_content_table(self):
        """Test that tables are detected as wide."""
        renderer = ResponseRenderer()

        table = """| Name | Age | City |
|------|-----|------|
| Alice | 30 | NYC |
| Bob | 25 | LA |"""
        assert renderer._is_wide_content(table) is True

    def test_is_wide_content_long_url(self):
        """Test that long URLs are detected as wide."""
        renderer = ResponseRenderer()

        long_url = "Check out https://example.com/very/long/path/that/exceeds/eighty/characters/and/should/be/wrapped/in/expandable/blockquote"
        assert renderer._is_wide_content(long_url) is True

    def test_is_wide_content_normal_text(self):
        """Test that normal text is not detected as wide."""
        renderer = ResponseRenderer()

        normal_text = """This is normal text.
It has multiple lines.
But none of them are very long.
And it doesn't have special characters."""
        assert renderer._is_wide_content(normal_text) is False

    def test_format_expandable_blockquote(self):
        """Test that content is wrapped in expandable blockquote syntax."""
        renderer = ResponseRenderer()

        content = "Line 1\nLine 2\nLine 3"
        formatted = renderer._format_expandable_blockquote(content)

        expected = "<blockquote>||Line 1\nLine 2\nLine 3||</blockquote>"
        assert formatted == expected

    def test_format_expandable_blockquote_whitespace(self):
        """Test that trailing whitespace is cleaned."""
        renderer = ResponseRenderer()

        content = "Line 1  \nLine 2\n\tLine 3  \n"
        formatted = renderer._format_expandable_blockquote(content)

        # Should remove trailing whitespace but keep structure
        assert "  \n" not in formatted
        assert "\t" not in formatted
        assert "<blockquote>||" in formatted
        assert "||</blockquote>" in formatted

    def test_format_expandable_blockquote_empty_lines(self):
        """Test that empty lines are preserved."""
        renderer = ResponseRenderer()

        content = "Line 1\n\nLine 3"
        formatted = renderer._format_expandable_blockquote(content)

        assert "Line 1\n\nLine 3" in formatted
        assert formatted.startswith("<blockquote>||")
        assert formatted.endswith("||</blockquote>")

    def test_format_expandable_blockquote_json(self):
        """Test formatting JSON as expandable blockquote."""
        renderer = ResponseRenderer()

        json_content = '{"status": "success", "data": {"id": 123, "name": "test"}}'
        formatted = renderer._format_expandable_blockquote(json_content)

        assert formatted.startswith("<blockquote>||")
        assert formatted.endswith("||</blockquote>")
        assert json_content in formatted
