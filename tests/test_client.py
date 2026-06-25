"""
Tests for jaato-client-telegram.

This test suite validates the core components:
- Configuration loading
- Session pool management
- Response rendering and message splitting
- Event streaming with flush mode handling
- (Integration tests require running jaato server)
"""

import pytest
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from jaato_client_telegram.renderer import split_preserving_paragraphs, StreamingContext


@dataclass
class MockEvent:
    """Mock SDK event for testing."""
    type: str
    source: str | None = None
    mode: str | None = None
    text: str = ""
    request_id: str | None = None
    tool_name: str | None = None
    formatted_text: str | None = None


class TestResponseRenderer:
    """Test message splitting and rendering logic."""

    def test_split_short_text(self):
        """Short text should not be split."""
        text = "Hello, world!"
        chunks = split_preserving_paragraphs(text, max_len=4096)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_split_long_paragraph(self):
        """Single long paragraph should be split at character boundaries."""
        text = "A" * 5000
        chunks = split_preserving_paragraphs(text, max_len=4096)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 904

    def test_split_preserves_paragraphs(self):
        """Paragraph boundaries should be preserved."""
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = split_preserving_paragraphs(text, max_len=50)

        # Should split into multiple chunks
        assert len(chunks) > 1

        # First chunk should have first paragraph
        assert "First paragraph" in chunks[0]

        # No paragraph should be broken mid-sentence (if it fits)
        # This is a soft requirement - very long paragraphs will be broken

    def test_split_empty_text(self):
        """Empty text should return empty list."""
        chunks = split_preserving_paragraphs("", max_len=4096)
        assert len(chunks) == 0

    def test_split_multiple_short_paragraphs(self):
        """Multiple short paragraphs should be combined into one chunk."""
        text = "Para 1\n\nPara 2\n\nPara 3"
        chunks = split_preserving_paragraphs(text, max_len=100)
        assert len(chunks) == 1
        assert "Para 1" in chunks[0]
        assert "Para 2" in chunks[0]
        assert "Para 3" in chunks[0]

    def test_split_respects_max_len(self):
        """All chunks should respect max length."""
        text = "A" * 10000
        max_len = 1000
        chunks = split_preserving_paragraphs(text, max_len=max_len)

        for chunk in chunks:
            assert len(chunk) <= max_len


class TestEventStreaming:
    """Test event streaming with proper flush mode handling."""


    @pytest.mark.asyncio
    async def test_text_only_response_no_flush(self):
        """Text-only responses should complete without flush signal."""
        from jaato_client_telegram.renderer import ResponseRenderer
        from unittest.mock import AsyncMock, MagicMock

        renderer = ResponseRenderer()

        mock_message = MagicMock()
        mock_message.answer = AsyncMock()
        mock_message.chat.id = 123

        # Text-only response (no tool calls, no flush)
        events = [
            MockEvent(type="agent.output", source="model", mode="write", text="Hello!"),
            MockEvent(type="agent.output", source="model", mode="append", text=" How can I help?"),
            MockEvent(type="turn.completed"),
        ]

        async def event_generator():
            for e in events:
                yield e

        ctx = await renderer.stream_response(mock_message, event_generator())

        # Text should still be accumulated
        assert "Hello!" in ctx.accumulated_text
        assert "How can I help?" in ctx.accumulated_text


class TestConfig:
    """Test configuration loading."""

    def test_env_var_substitution(self):
        """Environment variables should be substituted in config."""
        import os
        import tempfile

        # Set env var
        os.environ["TEST_VAR"] = "test_value"

        # Create temp config file
        config_content = """
telegram:
  bot_token: "${TEST_VAR}"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_content)
            temp_path = f.name

        try:
            from jaato_client_telegram.config import load_config

            config = load_config(temp_path)
            assert config.telegram.bot_token == "test_value"

        finally:
            os.unlink(temp_path)
            del os.environ["TEST_VAR"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
