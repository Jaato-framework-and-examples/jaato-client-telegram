# Example host tool — built by the jaato-client-telegram bot on user request via
# `register_tool` (self-extension). REFERENCE ONLY: real tools live in the bot's
# host_tools_dir (outside the repo, so the confined runner can't self-install).
# Copy/adapt this or build your own. See docs/features/host-tools.md.
#
# NOTE: this real bot-built tool shells out to `curl` via subprocess; a production
# version would use httpx, as the built-in show_image does. Kept as the bot wrote
# it, to show authentic self-extension output (the query is URL-encoded, so the
# single-quoted shell arg isn't injectable).

"""Tool: Search the internet for a public image and send it to the user."""

import json
import os
import re
import subprocess
import tempfile
import urllib.parse

TOOL_SCHEMA = {
    "name": "image_search",
    "description": (
        "Search the internet for a publicly available image matching a query, "
        "pick the best result, and send it to the Telegram user with a description. "
        "Uses DuckDuckGo image search (no API key required)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for (e.g. 'cute golden retriever puppy').",
            },
        },
        "required": ["query"],
    },
}

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _run(cmd: str, timeout: int = 15) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _get_vqd(query: str) -> str | None:
    encoded = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/?q={encoded}"
    raw = _run(f"curl -s -L -A 'Mozilla/5.0' '{url}'")
    if not raw:
        return None
    m = re.search(r"vqd=([\'\"]?)([\d-]+)\1", raw)
    return m.group(2) if m else None


def _search_images(query: str, max_results: int = 20) -> list[dict]:
    vqd = _get_vqd(query)
    if not vqd:
        return []

    encoded = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/i.js?q={encoded}&kl=us-en&l=us-en&p=1&s=0&vqd={vqd}"
    raw = _run(
        f"curl -s -L -A 'Mozilla/5.0' -H 'Referer: https://duckduckgo.com/' '{url}'"
    )
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    results = []
    for r in data.get("results", []):
        image_url = r.get("image", "")
        if not image_url:
            continue
        results.append({
            "image": image_url,
            "thumbnail": r.get("thumbnail", ""),
            "title": r.get("title", ""),
            "width": r.get("width", 0),
            "height": r.get("height", 0),
            "source": r.get("source", ""),
            "url": r.get("url", ""),
        })
        if len(results) >= max_results:
            break
    return results


def _is_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def _score_result(r: dict) -> int:
    score = 0
    url = r.get("image", "")
    if _is_image_url(url):
        score += 100
    if r.get("title"):
        score += 10
    area = r.get("width", 0) * r.get("height", 0)
    if area > 0:
        score += min(area // 10000, 50)
    if "thumb" not in url.lower() and "tse" not in url.lower():
        score += 20
    return score


def _pick_best_image(results: list[dict]) -> dict | None:
    if not results:
        return None
    scored = sorted(results, key=_score_result, reverse=True)
    return scored[0]


async def execute(args: dict, ctx) -> dict:
    query = args["query"].strip()
    if not query:
        return {"error": "Query must not be empty."}

    results = _search_images(query)
    if not results:
        return {
            "error": f"No image results found for '{query}'. Try a different search term.",
        }

    best = _pick_best_image(results)
    image_url = best["image"]
    title = best.get("title", "")
    source = best.get("source", "")

    # Download image to workspace so it can be shown inline via show_image
    ext = ".jpg"
    path_lower = image_url.lower()
    if ".png" in path_lower:
        ext = ".png"
    elif ".webp" in path_lower:
        ext = ".webp"
    elif ".gif" in path_lower:
        ext = ".gif"

    filename = f"image_search_{hash(query) % 100000}{ext}"
    file_path = os.path.join(tempfile.gettempdir(), filename)
    _run(f"curl -s -L -o '{file_path}' '{image_url}'", timeout=20)

    downloaded = os.path.exists(file_path) and os.path.getsize(file_path) > 100

    if not downloaded:
        thumb = best.get("thumbnail", "")
        if thumb:
            file_path = os.path.join(tempfile.gettempdir(), f"image_search_{hash(query) % 100000}_thumb{ext}")
            _run(f"curl -s -L -o '{file_path}' '{thumb}'", timeout=20)
            downloaded = os.path.exists(file_path) and os.path.getsize(file_path) > 100

    # Build description
    description = f"🔍 {query}"
    if title:
        description += f"\n📷 {title}"
    if source:
        description += f"\n📌 Source: {source}"

    result = {
        "image_url": image_url,
        "description": description,
        "query": query,
    }

    if downloaded:
        result["file_path"] = file_path

    return result
