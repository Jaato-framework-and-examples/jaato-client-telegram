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

# Telegram's upload cap for send_photo (we fetch remote images ourselves rather
# than relying on Telegram's flaky direct-URL fetch).
_MAX_PHOTO_BYTES = 10 * 1024 * 1024


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
    {
        "name": "register_tool",
        "description": (
            "Install a NEW host tool you have written, making it callable from "
            "your NEXT turn. Workflow: (1) write the tool to "
            "tool_drafts/<name>.py in the workspace — a module-level TOOL_SCHEMA "
            "dict (name = the file stem, description, JSON-schema parameters) "
            "plus `async def execute(args, ctx)` returning a dict; ctx.bot and "
            "ctx.chat_id let it talk to Telegram. (2) SHOW the user the code. "
            "(3) call register_tool(name). The user approves, then the bot "
            "installs and registers it. Use this to extend your own capabilities "
            "on request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Tool name = the draft file stem (tool_drafts/<name>.py).",
                },
            },
            "required": ["name"],
        },
        "category": "telegram",
        "timeout": 30000,
        # NOT auto-approved: the user must approve running new executable code.
        "auto_approve": False,
    },
    {
        "name": "service_manifest",
        "description": (
            "Maintain the session-startup service manifest: the list of host tools "
            "that should be ensured-running at the start of every session. The "
            "service_checklist prefetch renders this list into your prompt each "
            "session, so you start/health-check each entry on your first turn. "
            "action='add' registers a tool + the args to invoke it with at startup "
            "(replaces any existing entry for that tool); 'remove' drops a tool; "
            "'list' returns the current manifest. Example: service_manifest("
            "action='add', tool='approval_webhook', args={'action': 'start'})."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove", "list"]},
                "tool": {
                    "type": "string",
                    "description": "Host tool name to invoke at session start (required for add/remove).",
                },
                "args": {
                    "type": "object",
                    "description": "Arguments to invoke the tool with at session start, "
                    "e.g. {\"action\": \"start\"}. Used by 'add'; defaults to {}.",
                },
            },
            "required": ["action"],
        },
        "category": "telegram",
        "timeout": 30000,
        # Edits bot-owned config (a JSON manifest of already-installed, trusted
        # tools) — not new executable code — so no per-call approval needed.
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


def make_service_manifest_executor(workspace: "str | None"):
    """Executor for the ``service_manifest`` built-in.

    Maintains ``<workspace>/.jaato/service_manifest.json`` — the deterministic
    list of ``{tool, args}`` invocation specs the ``service_checklist`` prefetch
    reads at session-prep. The manifest lives in the workspace .jaato/ (NOT the
    host_tools_dir, which is outside the workspace where the runner-confined
    prefetch can't read it); closes over the configured workspace path.
    """
    def _path() -> Path:
        if not workspace:
            raise RuntimeError("workspace not configured (set jaato_ws.workspace)")
        return Path(workspace) / ".jaato" / "service_manifest.json"

    def _load(path: Path) -> list:
        return json.loads(path.read_text() or "[]") if path.is_file() else []

    def _save(path: Path, entries: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2) + "\n")

    async def executor(args: dict) -> dict:
        args = args or {}
        action = args.get("action")
        try:
            path = _path()
            entries = _load(path)
            if action == "list":
                return {"manifest": entries, "count": len(entries)}
            if action == "add":
                tool = args.get("tool")
                if not tool:
                    return {"error": "add requires 'tool'"}
                invoke_args = args.get("args") or {}
                entries = [e for e in entries if e.get("tool") != tool]  # replace
                entries.append({"tool": tool, "args": invoke_args})
                _save(path, entries)
                return {"ok": True, "added": {"tool": tool, "args": invoke_args}, "manifest": entries}
            if action == "remove":
                tool = args.get("tool")
                if not tool:
                    return {"error": "remove requires 'tool'"}
                remaining = [e for e in entries if e.get("tool") != tool]
                _save(path, remaining)
                return {"ok": True, "removed": tool, "manifest": remaining}
            return {"error": f"unknown action {action!r}; use add/remove/list"}
        except Exception as e:
            logger.exception("service_manifest failed")
            return {"error": str(e)}

    return executor


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
        # Telegram caps photo captions at 1024 chars; a longer caption makes
        # send_photo fail ("caption is too long") and the image never shows.
        if caption and len(caption) > 1024:
            caption = caption[:1021] + "…"
        logger.info(
            "show_image call: url=%r file_path=%r caption_len=%d",
            url[:80] or None, file_path or None, len(caption) if caption else 0,
        )

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
                return await _show_remote_image(bot, chat_id, url, caption)

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


async def _show_remote_image(
    bot: "Bot",
    chat_id: int,
    url: str,
    caption: "str | None",
) -> dict:
    """Fetch an image URL ourselves and render it inline.

    Telegram's own ``send_photo(photo=url)`` is unreliable for redirected /
    headered / hotlink-protected image URLs ("failed to get HTTP URL content"),
    so we download the bytes (following redirects, with a browser User-Agent),
    confirm it isn't an HTML page, size-guard it, and upload the bytes. Returns
    a result dict; on any failure the agent can fall back to pasting the link.
    """
    import httpx

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(
                url,
                headers={
                    # A realistic browser UA — some hosts block non-browser
                    # agents / hotlinking on image URLs.
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
    except Exception as e:
        return {"error": f"Could not download the image: {e}"}

    ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype.startswith("text/") or ctype.startswith("application/json"):
        return {
            "error": (
                f"That URL returned {ctype} (a web page), not a direct image. "
                f"Use the image's direct URL."
            )
        }

    data = resp.content
    if not data:
        return {"error": "The URL returned no data."}
    if len(data) > _MAX_PHOTO_BYTES:
        return {
            "error": (
                f"Image too large to display ({len(data) / 1024 / 1024:.1f}MB > 10MB). "
                f"Use send_to_telegram to deliver it as a file instead."
            )
        }

    # Filename extension hints Telegram at the format.
    sub = ctype.split("/", 1)[-1] if ctype.startswith("image/") else "jpg"
    sub = "jpg" if sub in ("jpeg", "jpg") else sub
    if sub not in ("jpg", "png", "gif", "webp"):
        sub = "jpg"

    from aiogram.types import BufferedInputFile
    logger.info("show_image remote: ctype=%s size=%d caption_len=%d",
                ctype, len(data), len(caption) if caption else 0)
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(data, filename=f"image.{sub}"),
            caption=caption,
        )
    except Exception as e:
        logger.warning("show_image: send_photo rejected: %s", e)
        return {"error": f"Telegram rejected the image: {e}"}
    logger.info("show_image: rendered inline (%d bytes)", len(data))
    return {"result": "shown"}


def register_host_tools(
    bot: "Bot",
    chat_id: int,
    file_config: FileSharingConfig,
) -> dict[str, object]:
    """Build the tool name → executor map for a session.

    Call this when a new session is created; ``SessionPool._assemble_host_tools``
    attaches each executor as the ``handler`` on the matching tool schema and
    passes the list to ``WSRecoveryClient.register_client_tools``.
    """
    return {
        "send_to_telegram": make_send_to_telegram_executor(bot, chat_id, file_config),
        "show_image": make_show_image_executor(bot, chat_id, file_config),
    }
