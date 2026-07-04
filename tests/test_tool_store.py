"""Consume side of the tool store: browse_tools + install_tool.

install_tool must verify the fetched bytes against the registry sha256 and stage
the EXACT verified bytes to tool_drafts/<name>.py (the agent then calls
register_tool, where the user reviews + approves). We monkeypatch each tool's
module-level _fetch so no network is touched.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from jaato_client_telegram.host_tool_loader import load_tool_file

BROWSE = "examples/host_tools/browse_tools.py"
INSTALL = "examples/host_tools/install_tool.py"

CODE = b"TOOL_SCHEMA = {}\nasync def execute(a, c):\n    return {}\n"
SHA = hashlib.sha256(CODE).hexdigest()
REGISTRY = {
    "version": 1,
    "tools": [
        {"name": "greeter", "file": "tools/greeter.py", "description": "Say hi",
         "deps": ["emoji"], "sha256": SHA, "provenance": {}},
    ],
}


def _mod(path, fetch):
    _schema, execute = load_tool_file(Path(path))
    execute.__globals__["_fetch"] = fetch
    return execute.__globals__


def _good_fetch(url):
    if url.endswith("registry.json"):
        return json.dumps(REGISTRY).encode()
    if url.endswith("tools/greeter.py"):
        return CODE
    raise RuntimeError(f"404 {url}")


# ---- browse_tools -----------------------------------------------------------

def test_browse_lists_tools_and_filters():
    g = _mod(BROWSE, _good_fetch)
    r = asyncio.run(g["execute"]({}, SimpleNamespace()))
    assert "greeter" in r["result"] and "Say hi" in r["result"]
    assert "emoji" in r["result"]  # deps surfaced
    r2 = asyncio.run(g["execute"]({"query": "nomatch"}, SimpleNamespace()))
    assert "No tools" in r2["result"]


def test_browse_store_unreachable():
    def boom(url):
        raise RuntimeError("dns fail")
    g = _mod(BROWSE, boom)
    r = asyncio.run(g["execute"]({}, SimpleNamespace()))
    assert "error" in r and "store" in r["error"].lower()


# ---- install_tool -----------------------------------------------------------

def test_install_verifies_and_stages_exact_bytes(tmp_path):
    g = _mod(INSTALL, _good_fetch)
    ctx = SimpleNamespace(workspace=str(tmp_path))
    r = asyncio.run(g["execute"]({"name": "greeter"}, ctx))
    assert "result" in r
    assert "register_tool(name='greeter')" in r["result"]
    assert "emoji" in r["result"]  # dep note
    draft = tmp_path / "tool_drafts" / "greeter.py"
    assert draft.read_bytes() == CODE  # exact verified bytes, not model-retyped


def test_install_rejects_sha_mismatch(tmp_path):
    def tampered(url):
        if url.endswith("registry.json"):
            return json.dumps(REGISTRY).encode()
        return CODE + b"# TAMPERED\n"  # different bytes -> sha mismatch
    g = _mod(INSTALL, tampered)
    r = asyncio.run(g["execute"]({"name": "greeter"}, SimpleNamespace(workspace=str(tmp_path))))
    assert "error" in r and "FAILED" in r["error"]
    assert not (tmp_path / "tool_drafts" / "greeter.py").exists()  # nothing staged


def test_install_unknown_tool(tmp_path):
    g = _mod(INSTALL, _good_fetch)
    r = asyncio.run(g["execute"]({"name": "ghost"}, SimpleNamespace(workspace=str(tmp_path))))
    assert "error" in r and "No tool" in r["error"]


def test_install_requires_workspace():
    g = _mod(INSTALL, _good_fetch)
    r = asyncio.run(g["execute"]({"name": "greeter"}, SimpleNamespace(workspace="")))
    assert "error" in r and "workspace" in r["error"].lower()


def test_install_rejects_unsafe_name(tmp_path):
    g = _mod(INSTALL, _good_fetch)
    r = asyncio.run(g["execute"]({"name": "../evil"}, SimpleNamespace(workspace=str(tmp_path))))
    assert "error" in r and "Invalid" in r["error"]


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
