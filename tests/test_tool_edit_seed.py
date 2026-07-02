"""register_tool(action='edit'): seed the workspace draft from installed source.

Lets the confined agent MODIFY an already-installed host tool whose source lives
outside its sandbox (host_tools_dir) — the bot copies the current source into
tool_drafts/<name>.py so the agent can read + edit it, then re-install.
"""

import asyncio

from jaato_client_telegram.config import JaatoWSConfig
from jaato_client_telegram.session_pool import SessionPool


def _pool(tmp_path, *, host_tools=True):
    ws = tmp_path / "workspace"
    ws.mkdir()
    # host_tools_dir MUST be OUTSIDE the workspace (that's the whole reason the
    # agent can't reach installed source itself).
    tools = tmp_path / "host_tools"
    tools.mkdir()
    cfg = JaatoWSConfig(
        url="ws://x",
        workspace=str(ws),
        host_tools_dir=str(tools) if host_tools else "",
    )
    return SessionPool(cfg), ws, tools


def test_edit_seeds_draft_from_installed_source(tmp_path):
    pool, ws, tools = _pool(tmp_path)
    (tools / "remind.py").write_text("INSTALLED SOURCE v1\n")

    res = asyncio.run(pool.seed_tool_draft(1, "remind"))

    assert "result" in res, res
    draft = ws / "tool_drafts" / "remind.py"
    assert draft.is_file()
    # Exact copy of the installed source — the agent edits THIS, not a rewrite.
    assert draft.read_text() == "INSTALLED SOURCE v1\n"


def test_edit_unknown_tool_errors(tmp_path):
    pool, ws, _ = _pool(tmp_path)
    res = asyncio.run(pool.seed_tool_draft(1, "does_not_exist"))
    assert "error" in res and "No installed host tool" in res["error"]
    assert not (ws / "tool_drafts" / "does_not_exist.py").exists()


def test_edit_disabled_without_host_tools_dir(tmp_path):
    pool, _, _ = _pool(tmp_path, host_tools=False)
    res = asyncio.run(pool.seed_tool_draft(1, "remind"))
    assert "error" in res and "disabled" in res["error"].lower()


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
