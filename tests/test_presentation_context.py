"""Tests for presentation context functionality."""

from jaato_client_telegram.session_pool import create_telegram_presentation_context


class TestPresentationContext:
    """Test Telegram presentation context declaration."""

    def test_presentation_context_structure(self):
        """Test that presentation context has all required fields."""
        ctx = create_telegram_presentation_context()

        # Required fields
        assert "content_width" in ctx
        assert "supports_markdown" in ctx
        assert "supports_tables" in ctx
        assert "supports_code_blocks" in ctx
        assert "supports_expandable_content" in ctx
        assert "client_type" in ctx

    def test_content_width_mobile(self):
        """Test that content width is set for mobile (45 chars)."""
        ctx = create_telegram_presentation_context()
        assert ctx["content_width"] == 45

    def test_tables_disabled(self):
        """Test that tables are marked as not supported."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_tables"] is False

    def test_expandable_content_enabled(self):
        """Test that expandable content support is enabled."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_expandable_content"] is True

    def test_client_type_chat(self):
        """Test that client type is set to 'chat'."""
        ctx = create_telegram_presentation_context()
        assert ctx["client_type"] == "chat"

    def test_markdown_enabled(self):
        """Test that basic markdown is supported."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_markdown"] is True

    def test_code_blocks_enabled(self):
        """Test that code blocks are supported."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_code_blocks"] is True

    def test_images_enabled(self):
        """Test that images are supported."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_images"] is True

    def test_mermaid_disabled(self):
        """Test that Mermaid diagrams are not supported."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_mermaid"] is False

    def test_rich_text_enabled(self):
        """Test that rich text formatting is supported."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_rich_text"] is True

    def test_unicode_enabled(self):
        """Test that Unicode is supported."""
        ctx = create_telegram_presentation_context()
        assert ctx["supports_unicode"] is True

    def test_content_height_none(self):
        """Test that content height is None (scrollable)."""
        ctx = create_telegram_presentation_context()
        assert ctx["content_height"] is None

    def test_all_boolean_fields(self):
        """Test that all capability flags are boolean."""
        ctx = create_telegram_presentation_context()

        bool_fields = [
            "supports_markdown",
            "supports_tables",
            "supports_code_blocks",
            "supports_images",
            "supports_rich_text",
            "supports_unicode",
            "supports_mermaid",
            "supports_expandable_content",
        ]

        for field in bool_fields:
            assert isinstance(ctx[field], bool), f"{field} should be boolean"

    def test_dict_serializable(self):
        """Test that context is JSON-serializable for SDK event."""
        import json

        ctx = create_telegram_presentation_context()

        # Should not raise exception
        json_str = json.dumps(ctx)
        assert isinstance(json_str, str)

        # Should round-trip correctly
        restored = json.loads(json_str)
        assert restored == ctx

    def test_expected_field_count(self):
        """Test that context has expected number of fields."""
        ctx = create_telegram_presentation_context()
        assert len(ctx) == 11  # All fields from design doc
