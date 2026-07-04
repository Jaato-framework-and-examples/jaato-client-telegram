"""Contribute side: share_tool opens a fork-and-PR to the store.

We mock the tool's _gh (GitHub REST), _token, and ctx.ask so no network / real
token is touched.
"""

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

from jaato_client_telegram.host_tool_loader import load_tool_file

SHARE = "examples/host_tools/share_tool.py"
REPO = "jaato-telegram-bot-tools-store"


def _mod():
    _schema, execute = load_tool_file(Path(SHARE))
    return execute.__globals__


def _fake_gh(calls):
    def gh(method, path, token, body=None):
        calls.append((method, path, body))
        if method == "GET" and path == "/user":
            return 200, {"login": "botacct"}
        if method == "POST" and path.endswith("/forks"):
            return 202, {}
        if method == "GET" and path == f"/repos/botacct/{REPO}":
            return 200, {"id": 1, "default_branch": "main"}
        if method == "GET" and "/git/ref/heads/main" in path:
            return 200, {"object": {"sha": "basesha"}}
        if method == "POST" and path.endswith("/git/refs"):
            return 201, {}
        if method == "GET" and "/contents/tools/" in path:
            return 404, {}  # new file
        if method == "PUT" and "/contents/tools/" in path:
            return 201, {}
        if method == "POST" and path.endswith("/pulls"):
            return 201, {"html_url": "https://github.com/x/pull/1"}
        return 500, {"message": f"unexpected {method} {path}"}
    return gh


def _ctx(htd, answer="Yes, open the PR"):
    async def ask(text, options, timeout=300):
        return answer
    return SimpleNamespace(host_tools_dir=str(htd), ask=ask)


def _with_tool(tmp_path):
    htd = tmp_path / "host_tools"
    htd.mkdir()
    (htd / "greeter.py").write_text(
        "TOOL_SCHEMA = {}\nasync def execute(a, c):\n    return {}\n"
    )
    return htd


def test_share_opens_pr_with_right_head_and_content(tmp_path):
    htd = _with_tool(tmp_path)
    g = _mod()
    calls = []
    g["_gh"] = _fake_gh(calls)
    g["_token"] = lambda: "tok"
    r = asyncio.run(g["execute"]({"name": "greeter", "note": "says hi"}, _ctx(htd)))
    assert "result" in r and "pull/1" in r["result"]

    pr = next(c for c in calls if c[0] == "POST" and c[1].endswith("/pulls"))
    assert pr[2]["head"] == "botacct:share-greeter"
    assert pr[2]["base"] == "main"
    assert "says hi" in pr[2]["body"] and "botacct" in pr[2]["body"]

    put = next(c for c in calls if c[0] == "PUT")
    assert base64.b64decode(put[2]["content"]).decode().startswith("TOOL_SCHEMA")


def _fake_gh_existing_pr(calls):
    """Re-run: branch + file already exist, PR already open → update path."""
    def gh(method, path, token, body=None):
        calls.append((method, path, body))
        if method == "GET" and path == "/user":
            return 200, {"login": "botacct"}
        if method == "POST" and path.endswith("/forks"):
            return 202, {}
        if method == "GET" and path == f"/repos/botacct/{REPO}":
            return 200, {"id": 1, "default_branch": "main"}
        if method == "GET" and "/git/ref/heads/main" in path:
            return 200, {"object": {"sha": "basesha"}}
        if method == "POST" and path.endswith("/git/refs"):
            return 422, {}  # branch already exists
        if method == "GET" and "/contents/tools/" in path:
            return 200, {"sha": "oldsha"}  # file already there
        if method == "PUT" and "/contents/tools/" in path:
            return 200, {}  # updated (new commit)
        if method == "POST" and path.endswith("/pulls"):
            return 422, {"message": "A pull request already exists"}
        if method == "GET" and "/pulls?head=" in path:
            return 200, [{"html_url": "https://github.com/x/pull/1", "number": 1}]
        return 500, {"message": f"unexpected {method} {path}"}
    return gh


def test_share_updates_existing_pr(tmp_path):
    htd = _with_tool(tmp_path)
    g = _mod()
    calls = []
    g["_gh"] = _fake_gh_existing_pr(calls)
    g["_token"] = lambda: "tok"
    r = asyncio.run(g["execute"]({"name": "greeter"}, _ctx(htd)))
    assert "result" in r
    assert "Updated the existing" in r["result"] and "pull/1" in r["result"]
    # it actually pushed a new commit (PUT with the existing file's sha)
    put = next(c for c in calls if c[0] == "PUT")
    assert put[2].get("sha") == "oldsha"


def test_share_disabled_without_token(tmp_path):
    htd = _with_tool(tmp_path)
    g = _mod()
    g["_token"] = lambda: ""
    r = asyncio.run(g["execute"]({"name": "greeter"}, _ctx(htd)))
    assert "error" in r and "token" in r["error"].lower()


def test_share_cancelled_opens_nothing(tmp_path):
    htd = _with_tool(tmp_path)
    g = _mod()
    calls = []
    g["_gh"] = _fake_gh(calls)
    g["_token"] = lambda: "tok"
    r = asyncio.run(g["execute"]({"name": "greeter"}, _ctx(htd, answer="Cancel")))
    assert "Cancelled" in r["result"]
    assert calls == []  # no GitHub calls at all


def test_share_no_installed_tool(tmp_path):
    htd = tmp_path / "host_tools"
    htd.mkdir()
    g = _mod()
    g["_token"] = lambda: "tok"
    r = asyncio.run(g["execute"]({"name": "ghost"}, _ctx(htd)))
    assert "error" in r and "No installed tool" in r["error"]


def test_share_unsafe_name(tmp_path):
    g = _mod()
    g["_token"] = lambda: "tok"
    r = asyncio.run(g["execute"]({"name": "../evil"}, _ctx(tmp_path)))
    assert "error" in r and "Invalid" in r["error"]


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
