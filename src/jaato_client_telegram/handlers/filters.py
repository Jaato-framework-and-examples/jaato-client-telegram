"""
Custom filters for aiogram handlers.

Provides reusable filters for group chat functionality.
"""

from aiogram import Bot
from aiogram.enums import MessageEntityType
from aiogram.filters import Filter
from aiogram.types import Message


class MentionedMe(Filter):
    """
    Filter that passes when the bot is mentioned in the message text.

    Supports:
    - @username mentions (e.g., @mybot hello)
    - text_mention entities (for users without usernames)
    - Replies to bot messages

    When the filter passes, it injects `mention_text` kwarg containing
    the message text with the mention removed.

    Example:
        @router.message(MentionedMe())
        async def handle_mention(message: Message, mention_text: str):
            await message.reply(f"You said: {mention_text}")
    """

    async def __call__(self, message: Message, bot: Bot) -> bool | dict:
        """
        Check if the bot is mentioned in the message.

        Args:
            message: Telegram message
            bot: Bot instance

        Returns:
            False if bot not mentioned, or dict with mention_text if mentioned
        """
        # First check: reply to bot message
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == bot.id:
                # This is a reply to our bot
                return {"mention_text": message.text or ""}

        # Second check: mention entities in message
        if not message.entities:
            return False

        me = await bot.me()

        for entity in message.entities:
            if entity.type == MessageEntityType.MENTION:
                # Extract mentioned username (skip the @ symbol)
                if not message.text:
                    continue
                username = message.text[entity.offset + 1 : entity.offset + entity.length]
                if username.lower() == me.username.lower():
                    # Bot was mentioned - extract clean text
                    clean_text = (
                        message.text[:entity.offset]
                        + message.text[entity.offset + entity.length :]
                    ).strip()
                    return {"mention_text": clean_text}

            elif entity.type == MessageEntityType.TEXT_MENTION:
                # text_mention for users without usernames
                if entity.user and entity.user.id == me.id:
                    if not message.text:
                        continue
                    clean_text = (
                        message.text[:entity.offset]
                        + message.text[entity.offset + entity.length :]
                    ).strip()
                    return {"mention_text": clean_text}

        return False
