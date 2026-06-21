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

# Image types Telegram renders inline via send_photo.
_DISPLAYABLE_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


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
    {
        "name": "show_image",
        "description": (
            "Display an image to the user INLINE in the chat (rendered, not a "
            "download). Pass `url` for an image you found on the web, or "
            "`file_path` for one saved in the workspace. For a downloadable file "
            "(full quality, any type), use send_to_telegram instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Direct image URL (http/https; jpg/png/gif/webp).",
                },
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to a workspace image to display.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption shown under the image.",
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
    send_exec = make_send_to_telegram_executor(bot, chat_id, file_config)
    show_exec = make_show_image_executor(bot, chat_id, file_config)
    return {
        "send_to_telegram": send_exec,
        "telegram_notify": send_exec,
        "show_image": show_exec,
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


def make_show_image_executor(
    bot: "Bot",
    chat_id: int,
    file_config: FileSharingConfig,
):
    """Create an executor for the show_image tool.

    Renders an image INLINE via ``bot.send_photo`` (vs send_to_telegram's
    download bubble). ``url`` is passed straight to Telegram, which fetches +
    renders it; ``file_path`` displays a workspace image. On failure the executor
    returns an error so the agent can fall back to pasting the link as text.
    """
    async def executor(args: dict) -> dict:
        url = (args.get("url") or "").strip()
        file_path = (args.get("file_path") or "").strip()
        caption = args.get("caption") or None

        try:
            if url and file_path:
                return {"error": "Provide either url or file_path, not both"}

            if file_path:
                return await _show_local_image(
                    bot, chat_id, file_path, caption, file_config
                )

            if url:
                if not (url.startswith("http://") or url.startswith("https://")):
                    return {"error": "url must be an http(s) image URL"}
                # Telegram fetches the URL and renders it inline. Limits: direct
                # image URL, ~5MB, Telegram-reachable — else send_photo raises and
                # we return the error for the agent to recover from.
                await bot.send_photo(chat_id=chat_id, photo=url, caption=caption)
                return {"result": "shown"}

            return {"error": "Provide url or file_path"}
        except Exception as e:
            logger.exception("show_image failed")
            return {"error": str(e)}

    return executor


async def _show_local_image(
    bot: "Bot",
    chat_id: int,
    file_path: str,
    caption: "str | None",
    config: FileSharingConfig,
) -> dict:
    """Render a workspace image inline via send_photo. Returns a result dict."""
    path = Path(file_path)

    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    if not config.enabled:
        return {"error": "File sharing is disabled"}

    ext = path.suffix.lower()
    if ext not in _DISPLAYABLE_IMAGE_EXTS:
        return {
            "error": (
                f"Not a displayable image type: {ext or '(none)'}. "
                f"Use send_to_telegram to deliver it as a file instead."
            )
        }

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > config.max_file_size_mb:
        return {
            "error": f"Image too large: {size_mb:.1f}MB > {config.max_file_size_mb}MB limit"
        }

    from aiogram.types import FSInputFile
    await bot.send_photo(
        chat_id=chat_id, photo=FSInputFile(path), caption=caption,
    )
    return {"result": f"shown {path.name}"}


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
        "show_image": make_show_image_executor(bot, chat_id, file_config),
    }
