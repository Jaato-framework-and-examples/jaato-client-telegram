"""
Private chat message handlers.

Handles messages from private (DM) conversations.
Each user gets their own isolated jaato SDK client session.
"""

import base64
import logging
from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.types import Message

from jaato_client_telegram.chat_pump import ChatPump, PumpItem
from jaato_client_telegram.clarification import ClarificationHandler, advance_clarification
from jaato_client_telegram.session_pool import SessionPool
from jaato_client_telegram.voice_mode_store import VOICE_HINT, VoiceModeStore
from jaato_client_telegram.voice_out import audio_to_wav

if TYPE_CHECKING:
    from jaato_client_telegram.rate_limiter import RateLimiter
    from jaato_client_telegram.abuse_protection import AbuseProtector
    from jaato_client_telegram.telemetry import TelemetryCollector


logger = logging.getLogger(__name__)

router = Router()


def _apply_voice_hint(
    store: "VoiceModeStore | None", chat_id: int, inbound_is_audio: bool, text: str,
) -> str:
    """Append the voice-reply directive when this turn should be spoken — the
    /voice toggle if set, else reply-in-kind (spoken iff the inbound was audio).
    The directive tells the model to enter the ``voz`` tier and say its answer."""
    if store is not None and store.wants_voice(chat_id, inbound_is_audio):
        return text + VOICE_HINT
    return text


@router.message(F.text, F.chat.type == "private")
async def handle_private_message(
    message: Message,
    pool: SessionPool,
    pump: ChatPump,
    clarification_handler: ClarificationHandler | None = None,
    voice_mode_store: VoiceModeStore | None = None,
    rate_limiter: "RateLimiter | None" = None,
    abuse_protector: "AbuseProtector | None" = None,
    telemetry: "TelemetryCollector | None" = None,
    admin_user_ids: list[int] | None = None,
) -> None:
    """Handle text messages from private chats.

    Runs access/abuse/rate checks, then hands the message to the per-chat pump,
    which owns the turn lifecycle. A message that arrives while a turn is still
    streaming is delivered mid-turn (steering) instead of blocking until the turn
    ends — see chat_pump.ChatPump.
    """
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id
    user_text = message.text

    if not user_text:
        return

    # If a clarification is awaiting this user's reply, route the text as the
    # answer to the current question instead of a new prompt. Handled here rather
    # than via the pump because it is a DISTINCT response type
    # (respond_to_clarification_batch) that unblocks the in-flight turn; the live
    # stream then renders the continuation.
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

    # Hand off to the per-chat pump: it owns session + turn + mid-turn steering.
    pump.submit(PumpItem(
        chat_id=chat_id, message=message,
        text=_apply_voice_hint(voice_mode_store, chat_id, False, user_text),
        apply_welcome=True, reply=False,
    ))


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


# Audio understanding is ON: the telegram_chat profile's DEFAULT tier
# (OpenRouter google/gemini-2.5-flash) declares `modalities: {audio: inbound}`,
# so a user voice/audio message is heard by the always-active model in ONE turn
# — no tier switch. Audio capability MUST live on the default tier: the audio
# arrives with the turn, so the model active when it lands has to be able to hear
# it (you can't reliably "switch into" an audio tier after the fact). Set False
# if the default tier drops its inbound-audio modality.
_AUDIO_ENABLED = True


def _build_audio_attachments(
    data: bytes, mime_type: str, name: str, seconds: "int | None" = None
) -> list[dict]:
    """Build the user-message attachment list for a voice/audio message.

    Same wire contract as images ({mime_type, data(base64), display_name}); the
    OpenRouter provider marshals an ``audio/*`` attachment into an ``input_audio``
    block (#830) for a tier that declares inbound audio. Telegram voice is
    OGG/Opus (``audio/ogg``, in the framework's format allow-list); audio files
    carry their own mime. ``seconds`` (optional) gives the model duration context.
    """
    att: dict = {
        "mime_type": mime_type,
        "data": base64.b64encode(data).decode("ascii"),
        "display_name": name,
    }
    if seconds is not None:
        att["seconds"] = seconds
    return [att]


@router.message((F.photo | F.document), F.chat.type == "private")
async def handle_private_media(
    message: Message,
    pool: SessionPool,
    pump: ChatPump,
    voice_mode_store: VoiceModeStore | None = None,
) -> None:
    """Handle an inbound photo or document.

    Images and PDFs go to the profile's vision tier as user-message attachments
    (the #353 ferry + #355 cross-provider tier, validated e2e). Any OTHER document
    (shell script, JSON, text, archive, …) is saved into the workspace's
    ``uploads/`` dir so the agent can read it with its file tools — no vision tier
    needed.
    """
    chat_id = message.chat.id

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

    is_vision = _is_vision_input(mime_type)
    is_pdf = mime_type == "application/pdf"
    kind = "PDF" if is_pdf else "image" if is_vision else "file"

    # The vision-disabled notice applies only to the vision path; non-vision
    # documents are staged to the workspace and don't need a vision model.
    if is_vision and not _VISION_ENABLED:
        await message.answer(
            "📷 Got your file — but image/PDF understanding isn't enabled yet. "
            "(It needs a vision-capable model; coming soon.) Send me text in the "
            "meantime."
        )
        return

    # Pre-check size against Telegram's 20 MB bot-download cap using the size
    # Telegram sends BEFORE download, so the user gets a clear reason rather than
    # a raw "file is too big" from get_file.
    size = getattr(tg_file, "file_size", None)
    if size and size > _MAX_DOWNLOAD_BYTES:
        await message.answer(
            f"📄 That {kind} is {size / 1024 / 1024:.0f} MB — I can only handle "
            f"files up to 20 MB."
        )
        return

    # Download + build the attachments/caption here (download errors handled
    # locally); the turn itself — send, stream, and mid-turn steering — is the
    # pump's job. The pre-download typing cue shows activity while we fetch.
    try:
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        tg_file_info = await message.bot.get_file(tg_file.file_id)
        buf = await message.bot.download_file(tg_file_info.file_path)
        data = buf.read()
    except Exception as e:  # noqa: BLE001 — download boundary
        logger.exception("Error downloading media from chat_id %s", chat_id)
        await message.answer(f"❌ Sorry, I couldn't download that {kind} — {e}")
        return

    if is_vision:
        # Image / PDF → vision-tier attachment (a PDF is read, not "seen").
        attachments = _build_image_attachments(data, mime_type, name)
        caption = (message.caption or "").strip() or (
            "Summarize this document." if is_pdf
            else "Describe what you see in this image."
        )
    else:
        # Any other document → stage into the workspace; the agent reads it with
        # its file tools (handles text AND binary, no context bloat).
        rel_path = pool.stage_upload(name, data)
        if rel_path is None:
            await message.answer(
                "📎 I can't save files right now — no workspace is "
                "configured for this bot."
            )
            return
        attachments = None
        user_q = (message.caption or "").strip()
        note = (
            f"📎 The user attached a file, saved to your workspace as "
            f"`{rel_path}`. Read it with your file tools to help."
        )
        caption = f"{user_q}\n\n{note}" if user_q else note

    logger.info(
        "inbound thread (media): chat=%s message_thread_id=%s vision=%s",
        chat_id, message.message_thread_id, is_vision,
    )
    pump.submit(PumpItem(
        chat_id=chat_id, message=message,
        text=_apply_voice_hint(voice_mode_store, chat_id, False, caption),
        attachments=attachments, apply_welcome=True, reply=False,
    ))


@router.message((F.voice | F.audio), F.chat.type == "private")
async def handle_private_audio(
    message: Message,
    pool: SessionPool,
    pump: ChatPump,
    clarification_handler: ClarificationHandler | None = None,
    voice_mode_store: VoiceModeStore | None = None,
) -> None:
    """Handle an inbound voice note or audio file.

    The audio rides to the profile's DEFAULT (audio-capable) tier as a
    user-message attachment (#830 ``input_audio`` ferry). Telegram *voice* is
    OGG/Opus (``audio/ogg``); an *audio* file carries its own mime. The default
    tier (gemini-2.5-flash, ``audio: inbound``) hears it in one turn — no tier
    switch, because audio arrives with the turn and the active model must be able
    to ingest it.
    """
    chat_id = message.chat.id

    # Resolve the Telegram audio object + its mime.
    if message.voice:
        tg_file = message.voice
        mime_type = tg_file.mime_type or "audio/ogg"       # Telegram voice = OGG/Opus
        name = f"voice_{tg_file.file_unique_id}.ogg"
    else:
        aud = message.audio
        tg_file = aud
        mime_type = aud.mime_type or "audio/mpeg"
        name = aud.file_name or f"audio_{aud.file_unique_id}"
    seconds = getattr(tg_file, "duration", None)

    if not _AUDIO_ENABLED:
        await message.answer(
            "🎤 Got your audio — but audio understanding isn't enabled yet. "
            "(It needs the default tier to declare inbound audio.) Send me text "
            "in the meantime."
        )
        return

    # Same 20 MB Telegram bot-download cap as other media (pre-checked on the
    # size Telegram sends, for a clear reason rather than a raw getFile error).
    size = getattr(tg_file, "file_size", None)
    if size and size > _MAX_DOWNLOAD_BYTES:
        await message.answer(
            f"🎤 That audio is {size / 1024 / 1024:.0f} MB — I can only handle "
            f"files up to 20 MB."
        )
        return

    try:
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        tg_file_info = await message.bot.get_file(tg_file.file_id)
        buf = await message.bot.download_file(tg_file_info.file_path)
        data = buf.read()
    except Exception as e:  # noqa: BLE001 — download boundary
        logger.exception("Error downloading audio from chat_id %s", chat_id)
        await message.answer(f"❌ Sorry, I couldn't download that audio — {e}")
        return

    # The voz ingest tier (OpenAI gpt-audio) rejects `input_audio.format: 'ogg'`
    # (accepts only wav/mp3), and Telegram voice notes are always OGG. Transcode
    # → WAV so the audio tier can hear them. Best-effort: if ffmpeg fails we send
    # the original OGG (which 400s — no worse than doing nothing).
    if mime_type == "audio/ogg":
        _wav = await audio_to_wav(data)
        if _wav:
            logger.info("inbound audio transcoded OGG->WAV (%d -> %d bytes)", len(data), len(_wav))
            data, mime_type = _wav, "audio/wav"
            name = (name.rsplit(".", 1)[0] if "." in name else name) + ".wav"

    attachments = _build_audio_attachments(data, mime_type, name, seconds)

    # If a clarification is awaiting this user's reply, answer THAT with the voice
    # note instead of starting a new turn (SDK #989: audio attached to a free_text
    # answer; the answer string is "" — the utterance IS the answer). Handled here,
    # like the text handler's clarification branch, because it unblocks the
    # in-flight turn rather than being a new prompt — WITHOUT this the voice note
    # became a fresh turn while the server still waited for the answer, hanging.
    if clarification_handler and clarification_handler.get_pending(chat_id) is not None:
        logger.info("inbound clarification answer (audio): chat=%s mime=%s", chat_id, mime_type)
        status, payload = clarification_handler.record_answer(chat_id, "", attachment=attachments)
        await advance_clarification(
            message, chat_id, status, payload, clarification_handler, pool,
        )
        return

    caption = (message.caption or "").strip() or "Listen to this voice message and respond."
    logger.info(
        "inbound thread (audio): chat=%s message_thread_id=%s mime=%s seconds=%s",
        chat_id, message.message_thread_id, mime_type, seconds,
    )
    # Reply-in-kind: a voice note gets a spoken reply by default (unless /voice off).
    pump.submit(PumpItem(
        chat_id=chat_id, message=message,
        text=_apply_voice_hint(voice_mode_store, chat_id, True, caption),
        attachments=attachments, apply_welcome=True, reply=False,
    ))
