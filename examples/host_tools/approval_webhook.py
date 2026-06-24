# Example host tool — built by the jaato-client-telegram bot on user request via
# `register_tool` (self-extension). REFERENCE ONLY: real tools live in the bot's
# host_tools_dir (outside the repo, so the confined runner can't self-install).
# Copy/adapt this or build your own. See docs/features/host-tools.md.

import json
import time
import uuid
from pathlib import Path

from aiohttp import web
from jaato_client_telegram.host_tool_loader import ask_user

DIR = Path(__file__).parent
LOG_FILE = DIR / "approval_webhook.log"
DEFAULT_PORT = 8090
ASK_TIMEOUT = 300  # 5 minutes

_state = {"runner": None, "port": None, "bot": None, "chat_id": None}


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] TOOL: {msg}\n")


TOOL_SCHEMA = {
    "name": "approval_webhook",
    "description": (
        "Exposes an HTTP endpoint where external systems POST approval requests. "
        "Notifies you in Telegram with inline buttons and returns your decision "
        "to the caller automatically. Actions: 'start' (launch server), "
        "'stop' (shut down), 'status' (check server health)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop", "status"],
                "description": "What to do with the webhook server.",
            },
            "port": {
                "type": "integer",
                "description": "Port to listen on (default 8090). Only used with 'start'.",
            },
        },
        "required": ["action"],
    },
    "timeout": 300000,
}


async def _handle_approve(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Echo the caller's id when provided so they can correlate the response to
    # their request; otherwise generate a short one.
    caller_id = payload.get("id")
    rid = str(caller_id) if caller_id else str(uuid.uuid4())[:8]
    bot = _state["bot"]
    chat_id = _state["chat_id"]

    _log(f"RECV rid={rid} payload={json.dumps(payload, default=str)[:500]}")

    summary = json.dumps(payload, indent=2, default=str)
    if len(summary) > 800:
        summary = summary[:800] + "..."

    choice = await ask_user(
        bot, chat_id,
        f"\U0001f4cb Approval Request <code>{rid}</code>\n\n"
        f"<pre>{summary}</pre>",
        ["Approve", "Deny"],
        timeout=ASK_TIMEOUT,
    )

    if choice == "Approve":
        _log(f"APPROVED rid={rid}")
        return web.json_response({"approved": True, "request_id": rid})
    elif choice == "Deny":
        _log(f"DENIED rid={rid}")
        return web.json_response({"approved": False, "request_id": rid}, status=403)
    else:
        _log(f"TIMEOUT rid={rid}")
        return web.json_response({"error": "timed out", "request_id": rid}, status=504)


async def _handle_status(request):
    return web.json_response({
        "status": "running",
        "port": _state["port"],
    })


async def execute(args: dict, ctx) -> dict:
    action = args.get("action")

    if action == "start":
        port = args.get("port", DEFAULT_PORT)

        if _state["runner"] is not None:
            return {"result": f"Server already running on port {_state['port']}."}

        _state["bot"] = ctx.bot
        _state["chat_id"] = ctx.chat_id
        _state["port"] = port

        app = web.Application()
        app.router.add_post("/approve", _handle_approve)
        app.router.add_get("/status", _handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()

        _state["runner"] = runner
        _log(f"SERVER started on port {port} (ask_user mode)")

        return {
            "result": (
                f"Approval webhook started on 127.0.0.1:{port}.\n"
                f"POST JSON to /approve \u2014 you'll get inline buttons to "
                f"approve or deny. No manual 'decide' action needed."
            )
        }

    elif action == "stop":
        if _state["runner"] is not None:
            await _state["runner"].cleanup()
            _state["runner"] = None
            _state["port"] = None
            _state["bot"] = None
            _state["chat_id"] = None
            _log("SERVER stopped")
            return {"result": "Approval webhook server stopped."}
        return {"result": "Server is not running."}

    elif action == "status":
        if _state["runner"] is not None:
            return {"result": f"Server running on 127.0.0.1:{_state['port']}."}
        return {"result": "Server not running."}

    return {"error": f"Unknown action: {action}"}
