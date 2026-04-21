"""
Bot lifecycle event handlers.

Handles bot being added to or removed from groups.
"""

import logging

from aiogram import Router
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import ChatMemberUpdated


logger = logging.getLogger(__name__)

router = Router()


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated) -> None:
    """
    Handle bot being added to a group or supergroup.

    Sends a welcome message explaining how to use the bot.
    """
    chat_title = event.chat.title or "this group"
    
    # Get bot username via API call
    try:
        bot_info = await event.bot.get_me()
        bot_username = bot_info.username or "bot"
    except Exception:
        bot_username = "bot"

    welcome_message = (
        f"👋 Hello {chat_title}!\n\n"
        f"I'm ready to help. You can:\n"
        f"• Mention me with @{bot_username} to ask questions\n"
        f"• Reply to my messages\n"
        f"• Use /help to see available commands\n\n"
        f"Each user gets their own isolated session, so your conversations "
        f"are private even in groups."
    )

    await event.answer(welcome_message)
    logger.info(f"Bot added to group: chat_id={event.chat.id}, title={chat_title}")


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed_from_group(event: ChatMemberUpdated) -> None:
    """
    Handle bot being removed from a group.

    Logs the removal for monitoring purposes. No message is sent since the bot
    no longer has permission to send messages in the chat.
    """
    chat_title = event.chat.title or "unknown"
    logger.info(
        f"Bot removed from group: chat_id={event.chat.id}, title={chat_title}"
    )
    # Note: Cannot send message here as bot was already removed
