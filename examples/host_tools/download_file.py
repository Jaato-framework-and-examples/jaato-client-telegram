# Example host tool — the direct "download a web file into the workspace" primitive.
# REFERENCE ONLY: real tools live in the bot's host_tools_dir (outside the repo, so
# the confined runner can't self-install). See docs/features/host-tools.md.
#
# Why this exists: the model has web_search (find URLs) and send_to_telegram (deliver
# a workspace file), but nothing that puts a web file ON DISK — so it used to hand-roll
# a download in a notebook cell. This is that missing primitive: one direct tool call
# fetches the URL into the workspace and returns the path to hand to send_to_telegram.
#
# Runs in the UNCONFINED bot (full network + filesystem), so it is deliberately narrow:
# http/https only, and the saved name is reduced to a bare basename so a file can only
# land INSIDE the workspace (no path traversal).

import asyncio
import os
import urllib.parse
import urllib.request

TOOL_SCHEMA = {
    "name": "download_file",
    "description": (
        "Download a file from a URL directly into your workspace and return its path. "
        "Use this to fetch any web file (PDF, image, document, archive…) so you can then "
        "deliver it with send_to_telegram(file_path=...). This is the direct way to "
        "download: do NOT hand-roll a download in notebook/cli, and do NOT use web_fetch "
        "(that only reads a page's text — it cannot save a file)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http/https URL of the file to download.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional name to save it as (basename only, keep the extension). "
                    "Defaults to the filename in the URL."
                ),
            },
        },
        "required": ["url"],
    },
}

# Some servers reject the default urllib agent with 403; present a browser-like UA.
_UA = "Mozilla/5.0 (compatible; jaato-telegram-bot/1.0)"


def _download(url: str, dest: str) -> None:
    """Blocking streamed download — run OFF the event loop (see execute)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)


async def execute(args, ctx):
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "url is required"}
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return {"error": "only http/https URLs are supported"}
    if not ctx.workspace:
        return {"error": "workspace not configured; cannot save the file"}

    # Pick the save name: explicit arg or the URL basename, reduced to a bare basename
    # so it can only land inside the workspace.
    raw = (args.get("filename") or "").strip() or urllib.parse.unquote(
        os.path.basename(urllib.parse.urlparse(url).path)
    )
    filename = os.path.basename(raw)
    if not filename:
        return {"error": "could not determine a filename from the URL; pass 'filename'"}

    dest = os.path.join(ctx.workspace, filename)
    try:
        # The download blocks; run it in a thread so the bot's single event loop
        # (which serves every chat and the Telegram poll) is never stalled.
        await asyncio.get_running_loop().run_in_executor(None, _download, url, dest)
    except Exception as e:  # surface a visible error the model can act on
        try:
            os.remove(dest)  # drop any partial file so a retry starts clean
        except OSError:
            pass
        return {"error": f"download failed: {e}"}

    size = os.path.getsize(dest)
    return {
        "result": (
            f"Downloaded '{filename}' ({size} bytes) into the workspace. "
            f'Deliver it with send_to_telegram(file_path="{filename}").'
        ),
        "file_path": filename,
    }
