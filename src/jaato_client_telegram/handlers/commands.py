"""
Command handlers for jaato-client-telegram.

Handles bot commands like /start, /reset, /status, and /help.
"""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from jaato_client_telegram.renderer import ResponseRenderer
from jaato_client_telegram.session_pool import SessionPool
from jaato_client_telegram.voice_mode_store import VoiceModeStore
from jaato_client_telegram.welcome_store import WELCOME_START


router = Router()


@router.message(Command("start"))
async def cmd_start(
    message: Message, pool: SessionPool, renderer: ResponseRenderer
) -> None:
    """
    Initialize a session and, on a chat's FIRST contact, stream a one-time
    model-generated welcome (the agent introduces itself + its capabilities/tools,
    which stays accurate as tools change). Returning users get a light reconnect.
    """
    chat_id = message.chat.id

    try:
        # Ensure a session exists (creates/re-attaches + connects if needed)
        session_id = await pool.get_or_create_session(chat_id)

        if pool.claim_first_contact(chat_id):
            # First time ever for this chat → let the AGENT introduce itself.
            await pool.send_message(session_id, WELCOME_START)
            await renderer.stream_response(
                initial_message=message,
                event_stream=await pool.events(session_id),
                thread_id_getter=lambda cid=chat_id: pool.current_thread(cid),
            )
        else:
            await message.answer(
                "✅ Reconnected. Send me a message anytime — /help for commands."
            )
    except Exception as e:
        await message.answer(
            f"❌ Failed to connect to jaato server.\n\n"
            f"Error: {e}\n\n"
            f"Please ensure the jaato server is running."
        )


@router.message(Command("voice"))
async def cmd_voice(
    message: Message,
    voice_mode_store: VoiceModeStore,
    command: CommandObject | None = None,
) -> None:
    """Control spoken replies: ``/voice on | off | auto``.

    - ``on``   → speak every reply (even to text)
    - ``off``  → text only
    - ``auto`` (default) → reply-in-kind: speak when you send a voice note.
    """
    arg = (command.args or "").strip().lower() if command else ""
    chat_id = message.chat.id
    if arg in ("on", "yes", "1"):
        voice_mode_store.set(chat_id, True)
        await message.answer(
            "🔊 Voice replies ON — I'll speak every reply. "
            "(/voice off for text, /voice auto for reply-in-kind.)"
        )
    elif arg in ("off", "no", "0"):
        voice_mode_store.set(chat_id, False)
        await message.answer(
            "🔇 Voice replies OFF — I'll reply in text. "
            "(/voice on to always speak, /voice auto for reply-in-kind.)"
        )
    elif arg in ("", "auto", "reset"):
        voice_mode_store.clear(chat_id)
        await message.answer(
            "🎙️ Voice replies AUTO — I'll speak when you send a voice note, "
            "text otherwise. (/voice on | off to override.)"
        )
    else:
        await message.answer("Usage: /voice on | off | auto")


@router.message(Command("reset"))
async def cmd_reset(message: Message, pool: SessionPool) -> None:
    """
    Reset the user's session.

    Disconnects and removes the current SDK client,
    creating a fresh session on the next message.
    """
    chat_id = message.chat.id

    # forget_session (not remove_client) so the persisted re-attach mapping is
    # ALSO dropped — otherwise the next message would re-attach the same session
    # and /reset wouldn't actually start fresh.
    await pool.forget_session(chat_id)

    await message.answer(
        "🔄 Session reset.\n\n"
        "Your conversation state has been cleared. "
        "Send a new message to start a fresh session."
    )


@router.message(Command("status"))
async def cmd_status(message: Message, pool: SessionPool) -> None:
    """
    Show client-level status information.

    Displays active session count and connection state.
    (This does NOT query the jaato server - it's client-side only.)
    """
    active_count = pool.active_count
    chat_id = message.chat.id

    # Check if this user has an active session
    session_info = pool.get_session_info(chat_id)

    status_lines = [
        "📊 jaato-client-telegram Status\n",
        f"Active sessions: {active_count}",
    ]

    if session_info:
        from datetime import datetime

        idle_seconds = (datetime.now() - session_info.last_activity).total_seconds()
        idle_minutes = int(idle_seconds / 60)

        status_lines.extend([
            "",
            f"Your session: ✅ Active",
            f"Idle time: {idle_minutes} minutes",
        ])
    else:
        status_lines.extend([
            "",
            f"Your session: ⚪ Not active",
            f"Send /start to connect",
        ])

    await message.answer("\n".join(status_lines))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """
    Show help information.

    Lists available commands and basic usage.
    """
    help_text = """🤖 jaato-client-telegram Help

<b>User Commands:</b>
/start - Connect to jaato and start a session
/reset - Reset your conversation session
/status - Show your session status
/help - Show this help message

<b>Admin Commands:</b>
/whitelist_add @user - Add user to whitelist
/whitelist_remove @user - Remove user from whitelist
/whitelist_list - List all whitelisted users
/whitelist_reload - Reload whitelist from file
/whitelist_status - Show whitelist status

<b>Usage:</b>
Just send me a message! I'll forward it to the jaato AI agent.

Each user gets an isolated conversation session. The agent can:
• Answer questions
• Execute tools (file operations, web search, etc.)
• Coordinate subagents for complex tasks

<b>Privacy:</b>
Conversations are isolated per user. Reset your session anytime with /reset.
"""

    await message.answer(help_text, parse_mode="HTML")
