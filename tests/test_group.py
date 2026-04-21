"""
Tests for group chat functionality.
"""

import pytest
from unittest.mock import Mock

from aiogram.types import (
    Message,
    Chat,
    User,
    MessageEntity,
)

from jaato_client_telegram.handlers.group import (
    _is_bot_mentioned,
    _has_trigger_prefix,
    _clean_message_text,
)


@pytest.fixture
def mock_bot():
    """Create a mock Bot instance."""
    bot = Mock()
    bot.user = Mock()
    bot.user.username = "testbot"
    return bot


@pytest.fixture
def mock_message(mock_bot):
    """Create a mock Message instance."""
    message = Mock(spec=Message)
    message.bot = mock_bot
    message.text = ""
    message.entities = []
    message.reply_to_message = None
    message.chat = Mock(spec=Chat)
    message.chat.id = 123456
    message.chat.type = "group"
    message.from_user = Mock(spec=User)
    message.from_user.id = 789
    return message


class TestIsBotMentioned:
    """Tests for _is_bot_mentioned function."""

    def test_username_mention_in_text(self, mock_message, mock_bot):
        """Test that @username mention in text is detected."""
        mock_message.text = "Hello @testbot, can you help?"
        assert _is_bot_mentioned(mock_message, "testbot") is True

    def test_no_mention(self, mock_message):
        """Test that message without mention returns False."""
        mock_message.text = "Hello everyone"
        assert _is_bot_mentioned(mock_message, "testbot") is False

    def test_wrong_bot_mentioned(self, mock_message):
        """Test that mention of different bot returns False."""
        mock_message.text = "Hello @otherbot"
        assert _is_bot_mentioned(mock_message, "testbot") is False

    def test_reply_to_bot(self, mock_message):
        """Test that reply to bot message is detected."""
        mock_message.text = "Thanks!"
        mock_message.reply_to_message = Mock()
        mock_message.reply_to_message.from_user = Mock()
        mock_message.reply_to_message.from_user.is_bot = True
        assert _is_bot_mentioned(mock_message, "testbot") is True

    def test_mention_entity(self, mock_message):
        """Test that mention entity is detected."""
        mock_message.text = "Hello @testbot"
        mock_message.entities = [
            MessageEntity(type="mention", offset=6, length=9)
        ]
        assert _is_bot_mentioned(mock_message, "testbot") is True


class TestHasTriggerPrefix:
    """Tests for _has_trigger_prefix function."""

    def test_with_prefix(self, mock_message):
        """Test that message with prefix is detected."""
        mock_message.text = "!ask what is the weather?"
        assert _has_trigger_prefix(mock_message, "!ask") is True

    def test_without_prefix(self, mock_message):
        """Test that message without prefix returns False."""
        mock_message.text = "what is the weather?"
        assert _has_trigger_prefix(mock_message, "!ask") is False

    def test_different_prefix(self, mock_message):
        """Test that different prefix is not detected."""
        mock_message.text = "/help"
        assert _has_trigger_prefix(mock_message, "!ask") is False

    def test_no_configured_prefix(self, mock_message):
        """Test that no configured prefix returns False."""
        mock_message.text = "any text"
        assert _has_trigger_prefix(mock_message, None) is False


class TestCleanMessageText:
    """Tests for _clean_message_text function."""

    def test_remove_bot_mention(self, mock_message):
        """Test that bot mention is removed."""
        mock_message.text = "@testbot hello there"
        result = _clean_message_text(mock_message, "testbot")
        assert result == "hello there"

    def test_remove_trigger_prefix(self, mock_message):
        """Test that trigger prefix is removed."""
        mock_message.text = "!ask what is this?"
        result = _clean_message_text(mock_message, "testbot", "!ask")
        assert result == "what is this?"

    def test_remove_both_mention_and_prefix(self, mock_message):
        """Test that both mention and prefix are removed."""
        mock_message.text = "@testbot !ask hello"
        result = _clean_message_text(mock_message, "testbot", "!ask")
        assert result == "hello"

    def test_empty_text(self, mock_message):
        """Test that empty text returns empty string."""
        mock_message.text = ""
        result = _clean_message_text(mock_message, "testbot")
        assert result == ""

    def test_none_text(self, mock_message):
        """Test that None text returns empty string."""
        mock_message.text = None
        result = _clean_message_text(mock_message, "testbot")
        assert result == ""

    def test_preserve_extra_whitespace(self, mock_message):
        """Test that extra whitespace is stripped."""
        mock_message.text = "@testbot   hello   there  "
        result = _clean_message_text(mock_message, "testbot")
        assert result == "hello   there"

    def test_no_mention_or_prefix(self, mock_message):
        """Test that text without mention or prefix is unchanged."""
        mock_message.text = "hello world"
        result = _clean_message_text(mock_message, "testbot")
        assert result == "hello world"
