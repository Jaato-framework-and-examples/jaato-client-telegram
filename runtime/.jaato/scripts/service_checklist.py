"""Prefetch: render the session-startup service checklist into the system prompt.

Reads the deterministic manifest at ``<workspace>/.jaato/service_manifest.json``
(maintained by the ``service_manifest`` host tool) and emits a checklist the model
runs on its first turn — ensuring each declared host tool is up. Re-rendered every
session, so a crashed service is restarted on the next interaction (self-healing).

Mandatory directive ({{!py:scripts/service_checklist.py}}): if this script raises
(e.g. malformed manifest JSON), session creation aborts with a structured error —
which is what we want for a load-bearing startup checklist. A missing or empty
manifest is NOT an error: it simply renders nothing.
"""

import json
from pathlib import Path


def render(context, args):
    ws = context.workspace_path
    if not ws:
        return ""
    path = Path(ws) / ".jaato" / "service_manifest.json"
    if not path.is_file():
        return ""
    entries = json.loads(path.read_text() or "[]")
    if not entries:
        return ""

    lines = [
        "## Session startup checklist",
        "",
        "Before responding to the user this session, ensure each service below is "
        "running. Invoke each host tool with the given arguments — the calls are "
        "idempotent (they confirm/report if already running), so this starts-if-down "
        "and confirms-if-up. Do this silently; only surface it if a service fails to start.",
        "",
    ]
    for entry in entries:
        tool = entry.get("tool")
        call_args = entry.get("args") or {}
        lines.append(f"- `{tool}` → call `{tool}({json.dumps(call_args)})`")
    return "\n".join(lines)
