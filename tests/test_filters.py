"""
Tests for custom filters.
"""

import pytest
from unittest.mock import Mock, AsyncMock

from aiogram.types import Message, User, Chat, MessageEntity
from aiogram.enums import MessageEntityType

from jaato_client_telegram.handlers.filters import MentionedMe


@pytest.fixture
def mock_bot():
    """Create a mock Bot instance."""
    bot = Mock()
    bot.id = 123456789
    bot.user = Mock()
    bot.user.username = "testbot"
    bot.user.id = 123456789
    # Make bot.me() return the bot user
    bot.me = AsyncMock(return_value=bot.user)
    return bot


@pytest.fixture
def mock_message(mock_bot):
    """Create a mock Message instance."""
    message = Mock(spec=Message)
    message.bot = mock_bot
    message.text = ""
    message.entities = None
    message.reply_to_message = None
    message.chat = Mock(spec=Chat)
    message.chat.id = 123456
    message.chat.type = "supergroup"
    message.from_user = Mock(spec=User)
    message.from_user.id = 987654321
    return message


class TestMentionedMeFilter:
    """Tests for MentionedMe filter."""

    @pytest.mark.asyncio
    async def test_username_mention_in_text(self, mock_message, mock_bot):
        """Test that @username mention in text is detected."""
        mock_message.text = "Hello @testbot, can you help?"
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is not False
        assert isinstance(result, dict)
        assert "mention_text" in result
        assert "Hello , can you help?" in result["mention_text"]

    @pytest.mark.asyncio
    async def test_no_mention(self, mock_message, mock_bot):
        """Test that message without mention returns False."""
        mock_message.text = "Hello everyone"
        mock_message.entities = []
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_wrong_bot_mentioned(self, mock_message, mock_bot):
        """Test that mention of different bot returns False."""
        mock_message.text = "Hello @otherbot"
        mock_message.entities = [
            MessageEntity(type=MessageEntityType.MENTION, offset=6, length=9)
        ]
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_reply_to_bot(self, mock_message, mock_bot):
        """Test that reply to bot message is detected."""
        mock_message.text = "Thanks!"
        mock_message.reply_to_message = Mock()
        mock_message.reply_to_message.from_user = Mock()
        mock_message.reply_to_message.from_user.id = mock_bot.id
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is not False
        assert isinstance(result, dict)
        assert "mention_text" in result
        assert result["mention_text"] == "Thanks!"

    @pytest.mark.asyncio
    async def test_reply_to_other_user(self, mock_message, mock_bot):
        """Test that reply to non-bot user returns False."""
        mock_message.text = "Thanks!"
        mock_message.reply_to_message = Mock()
        mock_message.reply_to_message.from_user = Mock()
        mock_message.reply_to_message.from_user.id = 999999999  # Different user
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_mention_entity(self, mock_message, mock_bot):
        """Test that mention entity is detected and extracted."""
        mock_message.text = "@testbot what is the weather?"
        mock_message.entities = [
            MessageEntity(type=MessageEntityType.MENTION, offset=0, length=9)
        ]
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is not False
        assert isinstance(result, dict)
        assert "mention_text" in result
        assert result["mention_text"] == "what is the weather?"

    @pytest.mark.asyncio
    async def test_text_mention_entity(self, mock_message, mock_bot):
        """Test that text_mention entity for bot is detected."""
        mock_message.text = "Can you help?"
        mock_message.entities = [
            MessageEntity(
                type=MessageEntityType.TEXT_MENTION,
                offset=0,
                length=12,
                user=Mock(id=mock_bot.id)
            )
        ]
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is not False
        assert isinstance(result, dict)
        assert "mention_text" in result

    @pytest.mark.asyncio
    async def test_text_mention_other_user(self, mock_message, mock_bot):
        """Test that text_mention for other user returns False."""
        mock_message.text = "Can you help?"
        mock_message.entities = [
            MessageEntity(
                type=MessageEntityType.TEXT_MENTION,
                offset=0,
                length=12,
                user=Mock(id=999999999)
            )
        ]
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_mention_middle_of_text(self, mock_message, mock_bot):
        """Test mention extraction when mention is in middle of text."""
        mock_message.text = "Hey @testbot can you help me?"
        mock_message.entities = [
            MessageEntity(type=MessageEntityType.MENTION, offset=4, length=9)
        ]
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is not False
        assert "mention_text" in result
        assert "Hey  can you help me?" in result["mention_text"]

    @pytest.mark.asyncio
    async def test_case_insensitive_mention(self, mock_message, mock_bot):
        """Test that mention is case insensitive."""
        mock_message.text = "@TestBot help"
        mock_message.entities = [
            MessageEntity(type=MessageEntityType.MENTION, offset=0, length=9)
        ]
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is not False
        assert "mention_text" in result

    @pytest.mark.asyncio
    async def test_empty_text(self, mock_message, mock_bot):
        """Test behavior with empty text."""
        mock_message.text = ""
        mock_message.entities = []
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_none_entities(self, mock_message, mock_bot):
        """Test behavior when entities is None."""
        mock_message.text = "Hello"
        mock_message.entities = None
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_multiple_mentions(self, mock_message, mock_bot):
        """Test handling of multiple mentions in one message."""
        mock_message.text = "@otherbot @testbot help"
        mock_message.entities = [
            MessageEntity(type=MessageEntityType.MENTION, offset=0, length=9),
            MessageEntity(type=MessageEntityType.MENTION, offset=10, length=9),
        ]
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        # Should detect the bot mention and extract text
        assert result is not False
        assert "mention_text" in result

    @pytest.mark.asyncio
    async def test_reply_to_bot_no_entities(self, mock_message, mock_bot):
        """Test reply to bot when entities is None."""
        mock_message.text = "Thanks!"
        mock_message.entities = None
        mock_message.reply_to_message = Mock()
        mock_message.reply_to_message.from_user = Mock()
        mock_message.reply_to_message.from_user.id = mock_bot.id
        
        mention_filter = MentionedMe()
        result = await mention_filter(mock_message, mock_bot)
        
        assert result is not False
        assert result["mention_text"] == "Thanks!"
