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


class TestSessionPool:
    """Test session pool logic."""

    @pytest.mark.asyncio
    async def test_session_pool_eviction(self):
        """Pool should evict oldest session when at capacity."""
        from jaato_client_telegram.session_pool import SessionPool
        from jaato_client_telegram.config import JaatoConfig
        from jaato_client_telegram.workspace import WorkspaceManager
        from unittest.mock import AsyncMock, MagicMock

        # Create a mock config
        config = JaatoConfig(socket_path="/tmp/test.sock")
        
        # Create a workspace manager
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            root.joinpath(".env").touch()
            root.joinpath(".jaato").mkdir()
            
            mock_config = MagicMock()
            workspace_manager = WorkspaceManager(mock_config)
            workspace_manager.root = root / "workspaces"
            workspace_manager.template_env = root / ".env"
            workspace_manager.template_jaato = root / ".jaato"

            # Create pool with max 2 sessions
            pool = SessionPool(config, workspace_manager, max_concurrent=2)

            # Mock the SDK client
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            mock_client.disconnect = AsyncMock()

            # Monkey-patch IPCRecoveryClient to return our mock
            import jaato_client_telegram.session_pool
            original_client = jaato_client_telegram.session_pool.IPCRecoveryClient

            def mock_init(*args, **kwargs):
                return mock_client

            jaato_client_telegram.session_pool.IPCRecoveryClient = mock_init

            try:
                # Create 2 sessions (at capacity)
                await pool.get_client(1)
                await pool.get_client(2)
                assert pool.active_count == 2

                # Create 3rd session - should evict oldest
                await pool.get_client(3)
                assert pool.active_count == 2

                # Session 1 should have been evicted
                assert pool.get_session_info(1) is None
                assert pool.get_session_info(3) is not None

            finally:
                # Restore original
                jaato_client_telegram.session_pool.IPCRecoveryClient = original_client

    @pytest.mark.asyncio
    async def test_session_cleanup_idle(self):
        """Pool should cleanup idle sessions."""
        from jaato_client_telegram.session_pool import SessionPool
        from jaato_client_telegram.config import JaatoConfig
        from jaato_client_telegram.workspace import WorkspaceManager
        from unittest.mock import AsyncMock
        from datetime import datetime, timedelta

        config = JaatoConfig(socket_path="/tmp/test.sock")
        
        # Create a workspace manager
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            root.joinpath(".env").touch()
            root.joinpath(".jaato").mkdir()
            
            mock_config = MagicMock()
            workspace_manager = WorkspaceManager(mock_config)
            workspace_manager.root = root / "workspaces"
            workspace_manager.template_env = root / ".env"
            workspace_manager.template_jaato = root / ".jaato"
            
            pool = SessionPool(config, workspace_manager, max_concurrent=10)

            # Mock client
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            mock_client.disconnect = AsyncMock()

            import jaato_client_telegram.session_pool
            original_client = jaato_client_telegram.session_pool.IPCRecoveryClient

            def mock_init(*args, **kwargs):
                return mock_client

            jaato_client_telegram.session_pool.IPCRecoveryClient = mock_init

            try:
                # Create session
                await pool.get_client(1)

                # Manually set last_activity to old time
                session = pool._sessions[1]
                session.last_activity = datetime.now() - timedelta(minutes=120)

                # Cleanup should remove idle session
                cleaned = await pool.cleanup_idle(max_idle_minutes=60)
                assert cleaned == 1
                assert pool.active_count == 0

            finally:
                jaato_client_telegram.session_pool.IPCRecoveryClient = original_client


class TestEventStreaming:
    """Test event streaming with proper flush mode handling."""

    @pytest.mark.asyncio
    async def test_flush_mode_triggers_text_display(self):
        """Flush signal should cause buffered text to be displayed."""
        from jaato_client_telegram.renderer import ResponseRenderer
        from unittest.mock import AsyncMock, MagicMock

        # Create renderer
        renderer = ResponseRenderer()

        # Mock message
        mock_message = MagicMock()
        mock_message.answer = AsyncMock()
        mock_message.chat.id = 123

        # Create event stream with proper sequence
        events = [
            # Model output starts
            MockEvent(type="agent.output", source="model", mode="write", text="Let me read that file."),
            # More chunks
            MockEvent(type="agent.output", source="model", mode="append", text=" Checking the contents..."),
            # Flush signal - text streaming done
            MockEvent(type="agent.output", source="model", mode="flush", text=""),
            # Tool execution starts
            MockEvent(type="tool.call.start", tool_name="readFile"),
            # Tool ends
            MockEvent(type="tool.call.end", tool_name="readFile"),
            # Turn completes
            MockEvent(type="turn.completed"),
        ]

        # Create async generator
        async def event_generator():
            for e in events:
                yield e

        # Stream events
        ctx = await renderer.stream_response(mock_message, event_generator())

        # Verify text was accumulated
        assert "Let me read that file." in ctx.accumulated_text
        assert "Checking the contents..." in ctx.accumulated_text

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

    @pytest.mark.asyncio
    async def test_multiple_flush_cycles(self):
        """Turn with multiple text→flush→tools cycles should handle correctly."""
        from jaato_client_telegram.renderer import ResponseRenderer
        from unittest.mock import AsyncMock, MagicMock

        renderer = ResponseRenderer()

        mock_message = MagicMock()
        mock_message.answer = AsyncMock()
        mock_message.chat.id = 123

        # Multiple cycles: text → flush → tools → text → flush → tools
        events = [
            # First cycle
            MockEvent(type="agent.output", source="model", mode="write", text="I'll read the file."),
            MockEvent(type="agent.output", source="model", mode="append", text=" One moment..."),
            MockEvent(type="agent.output", source="model", mode="flush", text=""),
            MockEvent(type="tool.call.start", tool_name="readFile"),
            MockEvent(type="tool.call.end", tool_name="readFile"),
            # Second cycle
            MockEvent(type="agent.output", source="model", mode="write", text="Now I'll update it."),
            MockEvent(type="agent.output", source="model", mode="flush", text=""),
            MockEvent(type="tool.call.start", tool_name="updateFile"),
            MockEvent(type="tool.call.end", tool_name="updateFile"),
            # Turn completes
            MockEvent(type="turn.completed"),
        ]

        async def event_generator():
            for e in events:
                yield e

        ctx = await renderer.stream_response(mock_message, event_generator())

        # All text should be accumulated
        assert "I'll read the file." in ctx.accumulated_text
        assert "One moment..." in ctx.accumulated_text
        assert "Now I'll update it." in ctx.accumulated_text

    @pytest.mark.asyncio
    async def test_buffers_cleared_on_flush(self):
        """Text buffer should be cleared when flush signal is received."""
        from jaato_client_telegram.renderer import ResponseRenderer
        from unittest.mock import AsyncMock, MagicMock

        renderer = ResponseRenderer()

        mock_message = MagicMock()
        mock_message.answer = AsyncMock()
        mock_message.chat.id = 123

        events = [
            MockEvent(type="agent.output", source="model", mode="write", text="Text before flush"),
            MockEvent(type="agent.output", source="model", mode="flush", text=""),
            MockEvent(type="agent.output", source="model", mode="write", text="Text after flush"),
            MockEvent(type="turn.completed"),
        ]

        async def event_generator():
            for e in events:
                yield e

        ctx = await renderer.stream_response(mock_message, event_generator())

        # Both text segments should be in accumulated_text
        assert "Text before flush" in ctx.accumulated_text
        assert "Text after flush" in ctx.accumulated_text

        # Buffer should be empty at the end
        assert len(ctx.text_buffer) == 0


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

    def test_default_values(self):
        """Config should have sensible defaults."""
        from jaato_client_telegram.config import Config

        # Create config from dict (missing optional fields)
        data = {
            "telegram": {
                "bot_token": "test_token",
            }
        }

        config = Config(**data)

        # Check defaults
        assert config.jaato.socket_path == "/tmp/jaato.sock"
        assert config.session.max_concurrent == 50
        assert config.rendering.max_message_length == 4096
        assert config.telegram.mode == "polling"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
