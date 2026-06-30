"""
Gmail OAuth2 Tool — read emails via Gmail API with OAuth2.

Actions:
  configure – save OAuth2 credentials (one-time setup)
  auth      – get authorization URL / exchange code for token
  check     – fetch unread emails (sender, subject, date, snippet)
  read      – fetch full body of a specific email by message ID
  watch     – start periodic polling, notify via Telegram on new emails
  unwatch   – stop periodic polling
  status    – connection health + last poll info

Credentials and tokens are stored in JSON files (workspace-local).
"""

import asyncio
import base64
import json
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

TOOL_SCHEMA = {
    "name": "gmail",
    "description": "Access a Gmail account via OAuth2/Gmail API. Actions: 'configure' (set OAuth2 client credentials), 'auth' (start OAuth flow or exchange code), 'check' (fetch unread emails), 'read' (full email by ID), 'watch' (periodic polling with Telegram notifications), 'unwatch' (stop polling), 'status' (connection health).",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["configure", "auth", "check", "read", "watch", "unwatch", "status"],
                "description": "What to do."
            },
            "client_id": {
                "type": "string",
                "description": "OAuth2 client ID. Required for 'configure'."
            },
            "client_secret": {
                "type": "string",
                "description": "OAuth2 client secret. Required for 'configure'."
            },
            "auth_code": {
                "type": "string",
                "description": "Authorization code from Google consent screen. For 'auth' action."
            },
            "message_id": {
                "type": "string",
                "description": "Gmail message ID to read. Required for 'read'."
            },
            "interval_minutes": {
                "type": "integer",
                "description": "Polling interval in minutes (default 5). Only for 'watch'."
            },
            "max_emails": {
                "type": "integer",
                "description": "Max emails to return (default 10). For 'check'."
            }
        },
        "required": ["action"]
    }
}

CONFIG_PATH = Path(__file__).parent / "gmail_oauth_config.json"
TOKEN_PATH = Path(__file__).parent / "gmail_oauth_token.json"
SEEN_PATH = Path(__file__).parent / "gmail_seen_ids.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://www.googleapis.com/gmail/v1/users/me"

_watch_task: asyncio.Task | None = None
_ctx = None
_last_poll = {"time": None, "count": 0, "new": 0}


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2))


def _load_seen() -> set[str]:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text()))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


def _save_seen(seen: set[str]):
    SEEN_PATH.write_text(json.dumps(sorted(seen)))


def _refresh_token(cfg: dict, token_data: dict) -> str:
    data = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URI, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
    token_data["access_token"] = result["access_token"]
    token_data["expiry"] = datetime.now().timestamp() + result.get("expires_in", 3600)
    _save_json(TOKEN_PATH, token_data)
    return token_data["access_token"]


def _get_access_token() -> str | None:
    cfg = _load_json(CONFIG_PATH)
    token_data = _load_json(TOKEN_PATH)
    if not cfg or not token_data:
        return None
    expiry = token_data.get("expiry", 0)
    if datetime.now().timestamp() > expiry - 60:
        return _refresh_token(cfg, token_data)
    return token_data["access_token"]


def _gmail_api_call(path: str, params: dict | None = None) -> dict:
    token = _get_access_token()
    if not token:
        raise RuntimeError("Not authenticated. Run 'auth' first.")
    url = f"{GMAIL_API}{path}"
    if params:
        sep = "&" if "?" in url else "?"
        url += sep + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


def _get_auth_url(cfg: dict) -> str:
    params = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": "http://localhost",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    })
    return f"{AUTH_URI}?{params}"


def _exchange_code(cfg: dict, code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "redirect_uri": "http://localhost",
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URI, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
    token_data = {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "expiry": datetime.now().timestamp() + result.get("expires_in", 3600),
    }
    _save_json(TOKEN_PATH, token_data)
    return token_data


def _decode_b64(data: str) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return data


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _fetch_unread(max_emails: int = 10) -> list[dict]:
    result = _gmail_api_call("/messages", params={
        "labelIds": "UNREAD",
        "maxResults": str(max_emails),
    })
    messages = result.get("messages", [])
    emails = []
    for msg_ref in messages:
        msg_id = msg_ref["id"]
        msg = _gmail_api_call(f"/messages/{msg_id}", params={"format": "full"})
        hdrs = msg.get("payload", {}).get("headers", [])
        emails.append({
            "id": msg_id,
            "sender": _get_header(hdrs, "From") or "unknown",
            "subject": _get_header(hdrs, "Subject") or "(no subject)",
            "date": _get_header(hdrs, "Date"),
            "snippet": msg.get("snippet", ""),
        })
    return emails


def _fetch_email(message_id: str) -> dict | None:
    msg = _gmail_api_call(f"/messages/{message_id}", params={"format": "full"})
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    body_parts = []
    _extract_parts(payload, body_parts)
    return {
        "id": message_id,
        "sender": headers.get("From", "unknown"),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "body": "\n".join(body_parts),
    }


def _extract_parts(part: dict, parts: list):
    mime = part.get("mimeType", "")
    if mime == "text/plain":
        data = part.get("body", {}).get("data")
        if data:
            parts.append(_decode_b64(data))
    elif "parts" in part:
        for sub in part["parts"]:
            _extract_parts(sub, parts)


async def _poll_loop(bot, chat_id: int, interval: int):
    global _last_poll
    cfg = _load_json(CONFIG_PATH)
    token_data = _load_json(TOKEN_PATH)
    if not cfg or not token_data:
        await bot.send_message(chat_id=chat_id, text="\u26a0 Gmail not authenticated. Use gmail auth first.")
        return
    seen = _load_seen()
    await bot.send_message(
        chat_id=chat_id,
        text=f"\U0001f4e7 Gmail watch started \u2014 polling every {interval} min."
    )
    while True:
        try:
            emails = _fetch_unread(max_emails=20)
            new_emails = [e for e in emails if e["id"] not in seen]
            _last_poll = {
                "time": datetime.now().isoformat(),
                "count": len(emails),
                "new": len(new_emails),
            }
            if new_emails:
                lines = [f"\U0001f4ec **{len(new_emails)} new email(s)**\n"]
                for e in new_emails:
                    lines.append(f"\u2022 _{e['sender']}_\n  {e['subject']}\n")
                await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
                for e in new_emails:
                    seen.add(e["id"])
                _save_seen(seen)
        except Exception as exc:
            await bot.send_message(chat_id=chat_id, text=f"\u26a0 Gmail poll error: {exc}")
        await asyncio.sleep(interval * 60)


async def execute(args: dict, ctx) -> dict:
    global _watch_task, _ctx, _last_poll
    _ctx = ctx
    action = args["action"]

    if action == "configure":
        client_id = args.get("client_id", "").strip()
        client_secret = args.get("client_secret", "").strip()
        if not client_id or not client_secret:
            return {"error": "Both 'client_id' and 'client_secret' are required."}
        cfg = {"client_id": client_id, "client_secret": client_secret}
        _save_json(CONFIG_PATH, cfg)
        return {"result": "\u2705 Credentials saved. Now run 'auth' to link your Gmail account."}

    if action == "auth":
        cfg = _load_json(CONFIG_PATH)
        if not cfg.get("client_id"):
            return {"error": "Not configured yet. You need a Google OAuth Desktop App client.\n\n"
                               "1. Go to console.cloud.google.com → APIs & Services → Credentials\n"
                               "2. Create a new OAuth client ID (type: Desktop app)\n"
                               "3. Make sure the Gmail API is enabled (APIs & Services → Library)\n"
                               "4. Run: gmail configure client_id=<YOUR_ID> client_secret=<YOUR_SECRET>"}
        auth_code = args.get("auth_code", "").strip()
        if auth_code:
            try:
                _exchange_code(cfg, auth_code)
                return {"result": "\u2705 Token saved! Gmail is now authenticated. Try 'check'."}
            except urllib.error.HTTPError as exc:
                body = json.loads(exc.read().decode()) if exc.readable() else {}
                err = body.get("error", str(exc))
                desc = body.get("error_description", "")
                hint = {
                    "invalid_grant": "The code expired or was already used. Run 'auth' again to get a fresh link.",
                    "invalid_client": "Client ID or secret is wrong. Check your Google Cloud Console credentials.",
                    "deleted_client": "This OAuth client was deleted from Google Cloud Console.",
                }.get(err, f"Token exchange failed: {err} — {desc}" if desc else f"Token exchange failed: {exc}")
                return {"error": hint}
            except Exception as exc:
                return {"error": f"Token exchange failed: {exc}"}
        else:
            url = _get_auth_url(cfg)
            return {"result": f"\U0001f511 Gmail authorization\n\n"
                               f"1. Open the link below in your browser\n"
                               f"2. Sign in and grant access\n"
                               f"3. The page will fail to load (that\'s normal)\n"
                               f"4. Copy the code after \'code=\' from the URL bar\n"
                               f"5. Paste it here with: gmail auth auth_code=<THE_CODE>\n\n"
                               f"{url}"}

    if action == "check":
        token = _get_access_token()
        if not token:
            return {"error": "Not authenticated. Run 'configure' with your Google OAuth credentials, then 'auth' to link your Gmail."}
        max_emails = args.get("max_emails", 10)
        try:
            emails = _fetch_unread(max_emails=max_emails)
            if not emails:
                return {"result": "\U0001f4ed No unread emails."}
            lines = []
            for e in emails:
                lines.append(f"  [{e['id']}] {e['sender']}")
                lines.append(f"      {e['subject']}")
                lines.append(f"      {e['snippet'][:150]}")
                lines.append("")
            return {"result": f"\U0001f4ec {len(emails)} unread email(s):\n\n" + "\n".join(lines)}
        except Exception as exc:
            return {"error": f"Gmail API error: {exc}"}

    if action == "read":
        token = _get_access_token()
        if not token:
            return {"error": "Not authenticated. Run 'configure' with your Google OAuth credentials, then 'auth' to link your Gmail."}
        message_id = args.get("message_id", "").strip()
        if not message_id:
            return {"error": "'message_id' is required for read action."}
        try:
            result = _fetch_email(message_id)
            if not result:
                return {"error": f"No email found with ID {message_id}."}
            return {
                "result": (
                    f"\U0001f4e7 **{result['subject']}**\n"
                    f"From: {result['sender']}\n"
                    f"To: {result['to']}\n"
                    f"Date: {result['date']}\n\n"
                    f"{result['body']}"
                )
            }
        except Exception as exc:
            return {"error": f"Gmail API error: {exc}"}

    if action == "watch":
        if _watch_task and not _watch_task.done():
            return {"error": "Already watching. Use 'unwatch' first to restart."}
        interval = args.get("interval_minutes", 5)
        _watch_task = asyncio.get_running_loop().create_task(
            _poll_loop(ctx.bot, ctx.chat_id, interval)
        )
        return {"result": f"\U0001f4e7 Watch started \u2014 polling every {interval} minutes."}

    if action == "unwatch":
        if _watch_task and not _watch_task.done():
            _watch_task.cancel()
            _watch_task = None
            return {"result": "\U0001f4e7 Watch stopped."}
        return {"result": "No active watch."}

    if action == "status":
        cfg = _load_json(CONFIG_PATH)
        token_data = _load_json(TOKEN_PATH)
        configured = bool(cfg.get("client_id"))
        authenticated = bool(token_data.get("refresh_token"))
        watching = _watch_task is not None and not _watch_task.done()
        poll_info = _last_poll.copy()
        return {
            "result": (
                f"\U0001f4e7 Gmail Status\n"
                f"  Configured: {'\u2705' if configured else '\u274c No'}\n"
                f"  Authenticated: {'\u2705' if authenticated else '\u274c No'}\n"
                f"  Watching: {'\u2705 Active' if watching else '\u274c Inactive'}\n"
                f"  Last poll: {poll_info.get('time', 'never')}\n"
                f"  Last unread: {poll_info.get('count', '-')}\n"
                f"  Last new: {poll_info.get('new', '-')}"
            )
        }

    return {"error": f"Unknown action: {action}"}
