"""
Command handlers for jaato-client-telegram.

Handles bot commands like /start, /reset, /status, and /help.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from jaato_client_telegram.session_pool import SessionPool


router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, pool: SessionPool) -> None:
    """
    Initialize a session for the user.

    Creates or retrieves an SDK client for this chat_id,
    establishing a connection to the jaato server.
    """
    chat_id = message.chat.id

    try:
        # Ensure client exists (triggers connection if needed)
        await pool.get_client(chat_id)

        await message.answer(
            "✅ Connected to jaato!\n\n"
            "Send me a message and I'll forward it to the AI agent. "
            "Each conversation is isolated and private.\n\n"
            "💡 Tip: Use /help to see available commands"
        )
    except Exception as e:
        await message.answer(
            f"❌ Failed to connect to jaato server.\n\n"
            f"Error: {e}\n\n"
            f"Please ensure the jaato server is running."
        )


@router.message(Command("reset"))
async def cmd_reset(message: Message, pool: SessionPool) -> None:
    """
    Reset the user's session.

    Disconnects and removes the current SDK client,
    creating a fresh session on the next message.
    """
    chat_id = message.chat.id

    await pool.remove_client(chat_id)

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
