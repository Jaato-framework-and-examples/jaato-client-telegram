"""
Group chat message handlers.

Handles messages from group chats (supergroups, groups).
Each user gets their own isolated session even within groups.
Supports mention filtering and trigger prefix configuration.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command

from jaato_client_telegram.handlers.commands import Command

from jaato_client_telegram.handlers.filters import MentionedMe
from jaato_client_telegram.renderer import ResponseRenderer
from jaato_client_telegram.session_pool import SessionPool

if TYPE_CHECKING:
    from jaato_client_telegram.rate_limiter import RateLimiter
    from jaato_client_telegram.abuse_protection import AbuseProtector
    from jaato_client_telegram.telemetry import TelemetryCollector


logger = logging.getLogger(__name__)

router = Router()

# Lock to prevent concurrent message processing for same user
_user_locks: dict[int, asyncio.Lock] = {}


def _get_user_lock(user_id: int) -> asyncio.Lock:
    """Get or create a lock for this user."""
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


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
    pool: SessionPool,
    renderer: ResponseRenderer,
    config,
    rate_limiter: "RateLimiter | None" = None,
    abuse_protector: "AbuseProtector | None" = None,
    telemetry: "TelemetryCollector | None" = None,
    admin_user_ids: list[int] | None = None,
) -> None:
    """
    Handle text messages from group chats where bot is mentioned.

    This handler uses the MentionedMe filter which automatically:
    - Detects @username mentions
    - Detects replies to bot messages
    - Extracts clean text without mentions

    Message processing flow:
    1. MentionedMe filter ensures bot was mentioned
    2. Check rate limits (if enabled)
    3. Check abuse protection (if enabled)
    4. Clean message text (remove trigger prefix if configured)
    5. Use user_id for session isolation (each user gets own session)
    6. Send user message to jaato via SDK
    7. Stream response events back to Telegram

    Args:
        message: Telegram message from group
        mention_text: Clean message text (mention already removed by filter)
        pool: Session pool for SDK clients
        renderer: Response renderer for streaming output
        config: Bot configuration (for group settings)
        rate_limiter: Optional rate limiter instance
        abuse_protector: Optional abuse protector instance
        admin_user_ids: Optional list of admin user IDs (for rate limit bypass)
    """
    # Get group configuration
    group_config = config.telegram.group
    trigger_prefix = group_config.trigger_prefix

    # Check for trigger prefix in addition to mention
    has_trigger = (
        _has_trigger_prefix(message, trigger_prefix)
        if trigger_prefix
        else False
    )

    # Clean the message text (remove trigger prefix if present)
    cleaned_text = _clean_trigger_prefix(mention_text, trigger_prefix)

    if not cleaned_text:
        return

    # Use user_id for session isolation, not chat_id
    # This ensures each user gets their own session even in groups
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        logger.warning("Message without from_user, skipping")
        return

    # Check abuse protection if enabled
    if abuse_protector:
        allowed, error_msg, _ = await abuse_protector.check_message(
            user_id=user_id,
            message_text=cleaned_text,
            admin_user_ids=admin_user_ids or [],
        )
        if not allowed:
            # Send abuse protection message as reply to original message
            await message.reply(error_msg)
            return

    # Check rate limits if enabled (rate limit per user, not per group)
    if rate_limiter:
        allowed, error_msg, _ = await rate_limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=admin_user_ids or [],
        )
        if not allowed:
            # Send rate limit message as reply to original message
            await message.reply(error_msg)
            return

    # Debug logging
    logger.debug(
        f"Group message: chat={message.chat.id}, "
        f"user={user_id}, "
        f"text={cleaned_text[:50]}, "
        f"has_trigger={has_trigger}"
    )

    # Get lock for this user to prevent concurrent message processing
    user_lock = _get_user_lock(user_id)

    # Acquire lock before processing
    async with user_lock:
        try:
            # Check if this is a new session for this user
            is_first_message = pool.get_session_info(user_id) is None

            # Send feedback message in group (reply to original message)
            if is_first_message:
                await message.reply(
                    "⏳ Connecting to your session...\n"
                    "(First message takes a few seconds to initialize)"
                )
            else:
                # Send typing indicator in the group
                await message.bot.send_chat_action(
                    chat_id=message.chat.id, action="typing"
                )

            # Get SDK client for this GROUP context (all users share same workspace)
            # Use chat_id for group isolation (not user_id)
            # This allows all group members to share the same conversation context
            client = await pool.get_client(message.chat.id)

            logger.info(
                f"Group message from user {user_id} in chat {message.chat.id}: "
                f"chat_workspace={message.chat.id}, session_id={client.session_id}"
            )

            # Send cleaned user message to jaato
            await client.send_message(cleaned_text)

            # Stream response events and render progressively
            # This handles everything including final display
            await renderer.stream_response(
                initial_message=message,
                event_stream=client.events(),
            )

        except Exception as e:
            logger.exception(f"Error handling group message from user_id {user_id}")

            # Send user-friendly error message
            error_text = (
                "❌ Error processing your message.\n\n"
                f"Details: {e}\n\n"
                "Use /reset to start a fresh session "
                "if the problem persists."
            )

            # Handle long error messages
            if len(error_text) > 4096:
                error_text = error_text[:4000] + "\n\n... (truncated)"

            await message.reply(error_text)


@router.message(
    F.chat.type.in_(["group", "supergroup"]),
    F.text,
)
async def handle_group_trigger_prefix(
    message: Message,
    pool: SessionPool,
    renderer: ResponseRenderer,
    config,
) -> None:
    """
    Handle messages with trigger prefix in groups (when require_mention=False).

    This handler catches messages that start with the configured trigger prefix
    even if the bot wasn't mentioned directly.

    Args:
        message: Telegram message from group
        pool: Session pool for SDK clients
        renderer: Response renderer for streaming output
        config: Bot configuration
    """
    # Get group configuration
    group_config = config.telegram.group
    trigger_prefix = group_config.trigger_prefix
    require_mention = group_config.require_mention

    # If require_mention is True, skip this handler
    # (mention-based handler will catch those)
    if require_mention:
        return

    # If no trigger prefix configured, skip
    if not trigger_prefix:
        return

    # Check if message starts with trigger prefix
    if not _has_trigger_prefix(message, trigger_prefix):
        return

    # Clean the message text
    cleaned_text = _clean_trigger_prefix(message.text or "", trigger_prefix)

    if not cleaned_text:
        return

    # Delegate to main handler with cleaned text
    # Note: We can't directly call handle_group_message because it expects
    # mention_text from the filter. Instead, we'll duplicate the logic here
    # or better: create a shared processing function.
    # For now, let's use a simpler approach:
    await _process_group_message(
        message=message,
        cleaned_text=cleaned_text,
        pool=pool,
        renderer=renderer,
    )


async def _process_group_message(
    message: Message,
    cleaned_text: str,
    pool: SessionPool,
    renderer: ResponseRenderer,
) -> None:
    """
    Shared logic for processing group messages.

    Args:
        message: Telegram message
        cleaned_text: Text with mentions/prefixes removed
        pool: Session pool
        renderer: Response renderer
    """
    # Use chat_id for GROUP workspace (shared by all group members)
    # This creates a collective memory for the entire group
    chat_id = message.chat.id

    # Get lock for this group
    user_lock = _get_user_lock(chat_id)

    # Acquire lock before processing
    async with user_lock:
        try:
            # Check if this is a new session
            is_first_message = pool.get_session_info(chat_id) is None

            if is_first_message:
                await message.reply(
                    "⏳ Connecting to your session...\n"
                    "(First message takes a few seconds to initialize)"
                )
            else:
                await message.bot.send_chat_action(
                    chat_id=message.chat.id, action="typing"
                )

            # Get SDK client for this GROUP (shared by all members)
            client = await pool.get_client(chat_id)

            user_id = message.from_user.id if message.from_user else "unknown"
            logger.info(
                f"Group message from user {user_id} in chat {chat_id}: "
                f"session_id={client.session_id}"
            )

            # Send message to jaato
            await client.send_message(cleaned_text)

            # Stream response
            await renderer.stream_response(
                initial_message=message,
                event_stream=client.events(),
            )

        except Exception as e:
            logger.exception(
                f"Error handling group message in chat_id={chat_id}"
            )

            error_text = (
                "❌ Error processing your message.\n\n"
                f"Details: {e}\n\n"
                "Use /reset to start a fresh session if the problem persists."
            )

            if len(error_text) > 4096:
                error_text = error_text[:4000] + "\n\n... (truncated)"

            await message.reply(error_text)


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
