"""Install a tool from the shared jaato tool store.

Fetches the chosen tool from
``Jaato-framework-and-examples/jaato-telegram-bot-tools-store``, verifies its
``sha256`` against the store registry, and stages the EXACT verified bytes to
``tool_drafts/<name>.py``. It does NOT install — you then call
``register_tool(name=...)``, where the user reviews the code and approves before
it runs (host tools run unconfined). See browse_tools + the design doc.
"""

import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

TOOL_SCHEMA = {
    "name": "install_tool",
    "description": (
        "Fetch a tool from the shared jaato store, verify its integrity, and stage "
        "it for install. Use browse_tools first to see names. AFTER this succeeds, "
        "call register_tool(name=...) — the user reviews the code and approves "
        "before it runs. Does not install anything on its own."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The tool name to install (as shown by browse_tools).",
            }
        },
        "required": ["name"],
    },
}
TOOL_DEPS = []  # stdlib only

_STORE = "Jaato-framework-and-examples/jaato-telegram-bot-tools-store"
_REF = os.environ.get("JAATO_TOOLSTORE_REF", "main")
_BASE = f"https://raw.githubusercontent.com/{_STORE}/{_REF}"
_SAFE = re.compile(r"^[a-z][a-z0-9_]*$")


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310 — fixed https host
        return r.read()


async def execute(args: dict, ctx) -> dict:
    name = (args.get("name") or "").strip()
    if not _SAFE.match(name):
        return {"error": f"Invalid tool name {name!r} (lowercase identifier)."}
    if not getattr(ctx, "workspace", ""):
        return {"error": "No workspace configured — can't stage the tool."}

    try:
        registry = json.loads(_fetch(f"{_BASE}/registry.json").decode())
    except Exception as e:  # noqa: BLE001
        return {"error": f"Couldn't reach the tool store: {e}"}

    entry = next((t for t in registry.get("tools", []) if t["name"] == name), None)
    if entry is None:
        return {"error": f"No tool {name!r} in the store — use browse_tools to list them."}

    try:
        code = _fetch(f"{_BASE}/{entry['file']}")
    except Exception as e:  # noqa: BLE001
        return {"error": f"Couldn't fetch {entry['file']}: {e}"}

    # Integrity of the fetch: the bytes must match the sha256 the reviewed store
    # published. (The PR review + the user's approval on register_tool are the
    # trust gates; this catches transit corruption / a wrong ref.)
    got = hashlib.sha256(code).hexdigest()
    if got != entry.get("sha256"):
        return {"error": (
            f"Integrity check FAILED for '{name}' (sha256 mismatch) — refusing to "
            f"stage it. The store ref may have moved; try again."
        )}

    drafts = Path(ctx.workspace) / "tool_drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / f"{name}.py").write_bytes(code)

    deps = entry.get("deps") or []
    dep_note = (
        f" It imports {', '.join(deps)} — if an import fails after install, "
        f"`pip install` them in a notebook cell." if deps else ""
    )
    return {"result": (
        f"Fetched '{name}' from the store and verified its sha256. Staged it to "
        f"tool_drafts/{name}.py. Now call register_tool(name='{name}') to install "
        f"it — the user will review the code and approve before it runs.{dep_note}"
    )}
