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

def _is_vision_input(mime_type: str) -> bool:
    """Attachment types the vision tier (OpenRouter gemini-2.5-flash) can read:
    images and PDFs. Both ride the same #353 ferry as base64 inline_data; the
    OpenRouter provider marshals image/* and application/pdf (both validated
    e2e). Other documents would need a separate staging path (not wired here).
    """
    return mime_type.startswith("image/") or mime_type == "application/pdf"


# Telegram bots can download files only up to 20 MB via getFile; larger files
# fail at download time. We pre-check the size Telegram sends with the message
# and tell the user clearly, rather than surfacing a raw "file is too big".
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def _build_image_attachments(data: bytes, mime_type: str, name: str) -> list[dict]:
    """Build the canonical user-message attachment list (image or PDF).

    Wire contract (per the framework multimodal ferry): a list of dicts
    ``{mime_type, data, display_name}`` where ``data`` is base64-encoded bytes.
    The daemon decodes them and builds inline_data parts for the vision-tier
    model — mime-agnostic, so images and application/pdf use the same path.
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
    """Handle an inbound photo, image, or PDF: download it from Telegram and
    deliver it to the agent as a user-message attachment so the profile's vision
    tier (OpenRouter gemini-2.5-flash) can describe an image or read a PDF.
    Validated e2e for both (the #353 ferry + #355 cross-provider tier).
    """
    chat_id = message.chat.id

    if not _VISION_ENABLED:
        await message.answer(
            "📷 Got your file — but image/PDF understanding isn't enabled yet. "
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

    if not _is_vision_input(mime_type):
        await message.answer(
            "📎 I can look at images and PDFs. Send a photo or a PDF and ask "
            "what you'd like to know about it."
        )
        return

    is_pdf = mime_type == "application/pdf"
    kind = "PDF" if is_pdf else "image"

    # Pre-check size against Telegram's 20 MB bot-download cap using the size
    # Telegram sends BEFORE download, so the user gets a clear reason rather than
    # a raw "file is too big" from get_file.
    size = getattr(tg_file, "file_size", None)
    if size and size > _MAX_DOWNLOAD_BYTES:
        await message.answer(
            f"📄 That {kind} is {size / 1024 / 1024:.0f} MB — I can only read "
            f"files up to 20 MB. "
            + ("Try splitting it or sending a smaller PDF."
               if is_pdf else "Try sending a smaller image.")
        )
        return

    # Default ask depends on the kind of file (a PDF is read, not "seen").
    caption = (message.caption or "").strip() or (
        "Summarize this document." if is_pdf else "Describe what you see in this image."
    )

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
            # Covers download failures and model-turn errors (e.g. a PDF with
            # too many pages for the vision model). Friendly + file-aware; the
            # full trace is logged, not dumped at the user.
            logger.exception("Error handling media from chat_id %s", chat_id)
            await message.answer(
                f"❌ Sorry, I couldn't process that {kind} — {e}"
            )
