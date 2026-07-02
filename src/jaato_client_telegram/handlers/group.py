"""
Group chat message handlers.

Handles messages from group chats (supergroups, groups).
Each user gets their own isolated session even within groups.
Supports mention filtering and trigger prefix configuration.
"""

import logging
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command

from jaato_client_telegram.handlers.commands import Command

from jaato_client_telegram.chat_pump import ChatPump, PumpItem
from jaato_client_telegram.handlers.filters import MentionedMe

if TYPE_CHECKING:
    from jaato_client_telegram.rate_limiter import RateLimiter
    from jaato_client_telegram.abuse_protection import AbuseProtector
    from jaato_client_telegram.telemetry import TelemetryCollector


logger = logging.getLogger(__name__)

router = Router()


def _has_trigger_prefix(message: Message, trigger_prefix: str) -> bool:
    """
    Check if message starts with the trigger prefix.

    Args:
        message: Telegram message
        trigger_prefix: Configured trigger prefix (e.g., "!ask", "/")

    Returns:
        True if message starts with prefix
    """
    if not trigger_prefix or not message.text:
        return False

    # Check if message starts with trigger prefix
    return message.text.startswith(trigger_prefix)


def _clean_trigger_prefix(text: str, trigger_prefix: str | None) -> str:
    """
    Remove trigger prefix from message text.

    Args:
        text: Message text
        trigger_prefix: Optional trigger prefix to remove

    Returns:
        Cleaned message text
    """
    if not text:
        return ""

    if trigger_prefix and text.startswith(trigger_prefix):
        text = text[len(trigger_prefix):].strip()

    return text


@router.message(F.chat.type.in_(["group", "supergroup"]), MentionedMe())
async def handle_group_message(
    message: Message,
    mention_text: str,
    pump: ChatPump,
    config,
    rate_limiter: "RateLimiter | None" = None,
    abuse_protector: "AbuseProtector | None" = None,
    telemetry: "TelemetryCollector | None" = None,
    admin_user_ids: list[int] | None = None,
) -> None:
    """Handle group messages where the bot is mentioned.

    Access/abuse/rate checks (per user), then hand off to the per-chat pump. The
    jaato session is keyed on chat.id (the group), so the pump serializes the
    group's turns and delivers a message that arrives mid-turn as a steer. (This
    also fixes the old per-USER lock, which let two members drive two concurrent
    turns on the one shared chat.id session.)
    """
    group_config = config.telegram.group
    trigger_prefix = group_config.trigger_prefix
    cleaned_text = _clean_trigger_prefix(mention_text, trigger_prefix)
    if not cleaned_text:
        return

    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        logger.warning("Group message without from_user, skipping")
        return

    # Check abuse protection if enabled (per user)
    if abuse_protector:
        allowed, error_msg, _ = await abuse_protector.check_message(
            user_id=user_id,
            message_text=cleaned_text,
            admin_user_ids=admin_user_ids or [],
        )
        if not allowed:
            await message.reply(error_msg)
            return

    # Check rate limits if enabled (per user, not per group)
    if rate_limiter:
        allowed, error_msg, _ = await rate_limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=admin_user_ids or [],
        )
        if not allowed:
            await message.reply(error_msg)
            return

    pump.submit(PumpItem(
        chat_id=message.chat.id, message=message, text=cleaned_text,
        apply_welcome=False, reply=True,
    ))


@router.message(
    F.chat.type.in_(["group", "supergroup"]),
    F.text,
)
async def handle_group_trigger_prefix(
    message: Message,
    pump: ChatPump,
    config,
) -> None:
    """Handle trigger-prefixed group messages when require_mention=False.

    Catches messages starting with the configured trigger prefix even without a
    direct mention, then hands off to the per-chat pump (same as the mention
    handler).
    """
    group_config = config.telegram.group
    trigger_prefix = group_config.trigger_prefix
    if group_config.require_mention:
        return
    if not trigger_prefix:
        return
    if not _has_trigger_prefix(message, trigger_prefix):
        return

    cleaned_text = _clean_trigger_prefix(message.text or "", trigger_prefix)
    if not cleaned_text:
        return

    pump.submit(PumpItem(
        chat_id=message.chat.id, message=message, text=cleaned_text,
        apply_welcome=False, reply=True,
    ))


@router.message(Command("help"), ~F.chat.type == "private")
async def cmd_group_help(message: Message, config) -> None:
    """
    Show help information in group chats.

    Displays usage instructions for group interactions.

    Args:
        message: Telegram message
        config: Bot configuration
    """
    group_config = config.telegram.group

    help_lines = [
        "🤖 <b>jaato-client-telegram Group Help</b>\n",
        "<b>How to use me:</b>\n",
    ]

    if group_config.require_mention:
        # Get bot username safely
        bot_username = "bot"
        if message.bot:
            try:
                bot_info = await message.bot.get_me()
                bot_username = bot_info.username or "bot"
            except Exception:
                bot_username = "bot"
        
        help_lines.append(
            f"• Mention me with @{bot_username} to get my attention\n"
        )
    else:
        help_lines.append("• Just send a message and I'll respond\n")

    if group_config.trigger_prefix:
        help_lines.append(
            f"• Or use the trigger prefix: <code>{group_config.trigger_prefix}</code>\n"
        )

    help_lines.extend([
        "",
        "<b>Session Isolation:</b>",
        "• Each user gets their own isolated session",
        "• Your conversations are private and separate from others",
        "",
        "<b>Commands:</b>",
        "/reset - Reset your session",
        "/help - Show this help message",
    ])

    await message.reply("\n".join(help_lines), parse_mode="HTML")
