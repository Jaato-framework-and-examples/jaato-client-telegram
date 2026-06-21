"""
Private chat message handlers.

Handles messages from private (DM) conversations.
Each user gets their own isolated jaato SDK client session.
"""

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.types import Message

from jaato_client_telegram.clarification import ClarificationHandler, advance_clarification
from jaato_client_telegram.renderer import ResponseRenderer
from jaato_client_telegram.session_pool import SessionPool

if TYPE_CHECKING:
    from jaato_client_telegram.rate_limiter import RateLimiter
    from jaato_client_telegram.abuse_protection import AbuseProtector
    from jaato_client_telegram.telemetry import TelemetryCollector


logger = logging.getLogger(__name__)

router = Router()

# Lock to prevent concurrent message processing for the same user
_user_locks: dict[int, asyncio.Lock] = {}


def _get_user_lock(chat_id: int) -> asyncio.Lock:
    """Get or create a lock for this user."""
    if chat_id not in _user_locks:
        _user_locks[chat_id] = asyncio.Lock()
    return _user_locks[chat_id]


@router.message(F.text, F.chat.type == "private")
async def handle_private_message(
    message: Message,
    pool: SessionPool,
    renderer: ResponseRenderer,
    clarification_handler: ClarificationHandler | None = None,
    rate_limiter: "RateLimiter | None" = None,
    abuse_protector: "AbuseProtector | None" = None,
    telemetry: "TelemetryCollector | None" = None,
    admin_user_ids: list[int] | None = None,
) -> None:
    """
    Handle text messages from private chats.

    This is the core message flow:
    1. Check rate limits (if enabled)
    2. Check abuse protection (if enabled)
    3. Get or create SDK client for this user
    4. Send user message to jaato via SDK
    5. Stream response events back to Telegram
    6. Render progressively with edit-in-place

    Args:
        message: Telegram message from user
        pool: Session pool for SDK clients
        renderer: Response renderer for streaming output
        rate_limiter: Optional rate limiter instance
        abuse_protector: Optional abuse protector instance
        admin_user_ids: Optional list of admin user IDs (for rate limit bypass)
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id
    user_text = message.text

    if not user_text:
        return

    # If a clarification is awaiting this user's reply, route the text as the
    # answer to the current question instead of a new prompt. This runs BEFORE
    # the per-user lock: the turn that asked the question is still streaming and
    # holds that lock, so acquiring it here would deadlock. The answer unblocks
    # the server and the in-flight stream renders the continuation.
    if clarification_handler and clarification_handler.get_pending(chat_id) is not None:
        status, payload = clarification_handler.record_answer(chat_id, user_text)
        await advance_clarification(
            message, chat_id, status, payload, clarification_handler, pool,
        )
        return

    # Check abuse protection if enabled
    if abuse_protector:
        allowed, error_msg, _ = await abuse_protector.check_message(
            user_id=user_id,
            message_text=user_text,
            admin_user_ids=admin_user_ids or [],
        )
        if not allowed:
            await message.answer(error_msg)
            return

    # Check rate limits if enabled
    if rate_limiter:
        allowed, error_msg, _ = await rate_limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=admin_user_ids or [],
        )
        if not allowed:
            await message.answer(error_msg)
            return

    # Get lock for this user to prevent concurrent message processing
    user_lock = _get_user_lock(chat_id)

    # Acquire lock before processing
    async with user_lock:
        try:
            # Check if this is a new session (first message from user)
            is_first_message = pool.get_session_info(chat_id) is None

            # IMMEDIATELY send feedback before any slow operations
            if is_first_message:
                # First message: warn about initialization time
                await message.answer(
                    "⏳ Connecting to your session...\n"
                    "(First message takes a few seconds to initialize)"
                )
            else:
                # Returning user: quick typing indicator
                await message.bot.send_chat_action(chat_id=chat_id, action="typing")

            session_id = await pool.get_or_create_session(chat_id)

            logger.debug(f"User {chat_id}: session_id = {session_id}")

            # Send user message to jaato
            await pool.send_message(session_id, user_text)

            # Stream response events and render progressively
            await renderer.stream_response(
                initial_message=message,
                event_stream=await pool.events(session_id),
            )

        except Exception as e:
            logger.exception(f"Error handling message from chat_id {chat_id}")

            # Check if this is a session-related error that might be transient
            # If so, provide a user-friendly message suggesting retry
            is_session_error = any(keyword in str(e).lower() for keyword in [
                'session', 'connection', 'disconnected', 'timeout'
            ])

            if is_session_error:
                error_text = (
                    f"❌ Connection or session issue detected.\n\n"
                    f"Details: {e}\n\n"
                    f"Please send your message again to retry with a fresh session."
                )
            else:
                error_text = (
                    f"❌ Error processing your message.\n\n"
                    f"Details: {e}\n\n"
                    f"Use /reset to start a fresh session if the problem persists."
                )

            # Handle long error messages
            if len(error_text) > 4096:
                error_text = error_text[:4000] + "\n\n... (truncated)"

            await message.answer(error_text)


# Image understanding is ON: the telegram_chat profile carries a real vision
# tier (OpenRouter google/gemini-2.5-flash) via V2 cross-provider model_tiers,
# so a user-message image is ferried (#353) to a model that actually sees it.
# Set back to False if the profile drops its vision tier.
_VISION_ENABLED = True

# Image MIME types are attached for vision; other documents would be staged
# into the workspace (a separate, non-vision path — not wired here yet).
_IMAGE_MIME_PREFIX = "image/"


def _build_image_attachments(data: bytes, mime_type: str, name: str) -> list[dict]:
    """Build the canonical user-message image attachment list.

    Wire contract (per the framework multimodal ferry): a list of dicts
    ``{mime_type, data, display_name}`` where ``data`` is base64-encoded bytes.
    The daemon decodes them and builds image parts for the vision-tier model.
    """
    return [{
        "mime_type": mime_type,
        "data": base64.b64encode(data).decode("ascii"),
        "display_name": name,
    }]


@router.message((F.photo | F.document), F.chat.type == "private")
async def handle_private_media(
    message: Message,
    pool: SessionPool,
    renderer: ResponseRenderer,
) -> None:
    """Handle an inbound photo / image document: download it from Telegram and
    deliver it to the agent as a user-message image attachment, so the profile's
    vision tier (glm-4.5v) can describe it.

    NOTE: end-to-end delivery depends on the framework multimodal ferry
    (user-message attachments → runner-tier model), which is a separate
    framework change. The bot side is complete and uses the canonical
    ``{mime_type, data: base64, display_name}`` contract.
    """
    chat_id = message.chat.id
    caption = (message.caption or "").strip() or "Describe what you see in this image."

    if not _VISION_ENABLED:
        await message.answer(
            "📷 Got your image — but image understanding isn't enabled yet. "
            "(It needs a vision-capable model; coming soon.) Send me text in the "
            "meantime."
        )
        return

    # Resolve the Telegram file + its MIME type.
    if message.photo:
        tg_file = message.photo[-1]          # largest rendition
        mime_type = "image/jpeg"             # Telegram re-encodes photos as JPEG
        name = f"photo_{tg_file.file_unique_id}.jpg"
    else:
        doc = message.document
        tg_file = doc
        mime_type = (doc.mime_type or "application/octet-stream")
        name = doc.file_name or "file"

    if not mime_type.startswith(_IMAGE_MIME_PREFIX):
        await message.answer(
            "📎 I can currently only look at images. Send a photo or an image "
            "file and ask what you'd like to know about it."
        )
        return

    user_lock = _get_user_lock(chat_id)
    async with user_lock:
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="typing")
            tg_file_info = await message.bot.get_file(tg_file.file_id)
            buf = await message.bot.download_file(tg_file_info.file_path)
            data = buf.read()
            attachments = _build_image_attachments(data, mime_type, name)

            session_id = await pool.get_or_create_session(chat_id)
            await pool.send_message(session_id, caption, attachments=attachments)
            await renderer.stream_response(
                initial_message=message,
                event_stream=await pool.events(session_id),
            )
        except Exception as e:
            logger.exception("Error handling media from chat_id %s", chat_id)
            await message.answer(
                f"❌ Error processing your image.\n\nDetails: {e}\n\n"
                f"Use /reset to start a fresh session if this persists."
            )
