"""Browse the shared jaato tool store.

Lists the host tools available to install from
``Jaato-framework-and-examples/jaato-telegram-bot-tools-store`` (the curated,
PR-reviewed store). Read-only — the user picks one and you then call
``install_tool``. See jaato-client-telegram/docs/design/tool-sharing-marketplace.md.
"""

import json
import os
import urllib.request

TOOL_SCHEMA = {
    "name": "browse_tools",
    "description": (
        "List host tools available in the shared jaato tool store (name, what "
        "each does, and any deps). Read-only. The user picks one, then you call "
        "install_tool(name=...) to install it (they review the code + approve)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional substring to filter by name/description.",
            }
        },
    },
}
TOOL_DEPS = []  # stdlib only

# The curated store + the ref to read. Pin to a tag/commit via env for a stronger
# guarantee; defaults to main (integrity of the fetch itself is checked by
# install_tool via sha256; the PR review + user acceptance are the trust gates).
_STORE = "Jaato-framework-and-examples/jaato-telegram-bot-tools-store"
_REF = os.environ.get("JAATO_TOOLSTORE_REF", "main")
_BASE = f"https://raw.githubusercontent.com/{_STORE}/{_REF}"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310 — fixed https host
        return r.read()


async def execute(args: dict, ctx) -> dict:
    q = (args.get("query") or "").strip().lower()
    try:
        registry = json.loads(_fetch(f"{_BASE}/registry.json").decode())
    except Exception as e:  # noqa: BLE001
        return {"error": f"Couldn't reach the tool store: {e}"}

    tools = registry.get("tools", [])
    if q:
        tools = [
            t for t in tools
            if q in t["name"].lower() or q in t.get("description", "").lower()
        ]
    if not tools:
        return {"result": "No tools in the store" + (f" matching {q!r}." if q else ".")}

    lines = [f"🧰 {len(tools)} tool(s) available in the store (ref: {_REF}):", ""]
    for t in tools:
        deps = f"  ·  needs: {', '.join(t['deps'])}" if t.get("deps") else ""
        lines.append(f"• *{t['name']}* — {t.get('description', '').strip()}{deps}")
    lines.append("")
    lines.append("Install one with install_tool(name='<name>').")
    return {"result": "\n".join(lines)}
