"""Host-provided tools for jaato-client-telegram.

Registers tools that the jaato model can call to send files and
messages to the Telegram user.  The transport dispatches
``tool.execute_request`` events from the server to async
executors defined here.

Tool registration happens once per WebSocket connection (in
``register_host_tools``).  Executors are stateless — they receive
a Bot instance and a target chat_id via a factory so the same
tool works across multiple sessions sharing one WS connection.
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from jaato_client_telegram.config import FileSharingConfig

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


TOOL_SCHEMAS = [
    {
        "name": "send_to_telegram",
        "description": (
            "Send a file or text message to the Telegram user. "
            "Use this to proactively share files you created (CSV, JSON, "
            "plots, etc.) or to send important notifications. "
            "Do NOT use this for ordinary conversation — that flows "
            "through the normal response channel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to send.",
                },
                "message": {
                    "type": "string",
                    "description": "Text to send. Ignored when file_path is provided.",
                },
            },
        },
        "category": "telegram",
        "timeout": 30000,
        "auto_approve": True,
    },
]

TOOL_CATEGORIES = {
    "telegram": "Telegram client-side communication tools",
}


def create_tool_executors(bot, chat_id: int, file_config) -> dict:
    exec = make_send_to_telegram_executor(bot, chat_id, file_config)
    return {
        "send_to_telegram": exec,
        "telegram_notify": exec,
    }


def make_send_to_telegram_executor(
    bot: "Bot",
    chat_id: int,
    file_config: FileSharingConfig,
):
    """Create an executor for the send_to_telegram tool.

    Returns an async callable ``(args: dict) -> dict``.
    """
    async def executor(args: dict) -> dict:
        file_path = args.get("file_path", "")
        message = args.get("message", "")

        try:
            if file_path:
                result = await _send_file(bot, chat_id, file_path, file_config)
                return {"result": result}

            if message:
                await bot.send_message(chat_id=chat_id, text=message)
                return {"result": "sent"}

            return {"error": "Provide file_path or message"}
        except Exception as e:
            logger.exception("send_to_telegram failed")
            return {"error": str(e)}

    return executor


async def _send_file(
    bot: "Bot",
    chat_id: int,
    file_path: str,
    config: FileSharingConfig,
) -> str:
    """Send a file to the Telegram user.  Returns a status string."""
    path = Path(file_path)

    if not path.exists():
        return f"File not found: {file_path}"

    if not config.enabled:
        return "File sharing is disabled"

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > config.max_file_size_mb:
        return f"File too large: {size_mb:.1f}MB > {config.max_file_size_mb}MB limit"

    ext = path.suffix.lower()
    if ext not in config.allowed_extensions:
        return f"File type not supported: {ext}"

    from aiogram.types import FSInputFile
    await bot.send_document(
        chat_id=chat_id,
        document=FSInputFile(path),
        caption=f"\U0001f4c4 {path.name}",
    )
    return f"sent {path.name} ({size_mb:.1f}MB)"


def register_host_tools(
    bot: "Bot",
    chat_id: int,
    file_config: FileSharingConfig,
) -> dict[str, object]:
    """Build the tool name → executor map for a session.

    Call this when a new session is created, then pass the result
    to ``WSTransport.set_tool_executor`` for each entry.
    """
    return {
        "send_to_telegram": make_send_to_telegram_executor(bot, chat_id, file_config),
    }
