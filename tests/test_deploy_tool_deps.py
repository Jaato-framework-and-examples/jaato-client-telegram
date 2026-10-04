"""Host tools declare their PyPI deps (TOOL_DEPS), and deploy-vps.sh installs
them into the workspace tool-venv.

Regression: until the bot had its own venv, host tools ran inside the framework
venv and borrowed its packages, so moon_phase (numpy), ttt (pillow) and the
VPS-installed image_search (httpx) never said what they import. Moved into an
sdk-only venv they stopped loading. youtube_search imports lazily, so it loaded
and would have failed on its first call.

deploy-vps.sh is sourced (its ``main`` only runs when executed); ``uv`` and the
venv creation are stubbed.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy-vps.sh"
EXAMPLES = REPO / "examples" / "host_tools"

# Import names whose PyPI distribution is spelled differently. Every other
# third-party import must appear in TOOL_DEPS under its own name.
DIST_FOR_IMPORT = {"PIL": "pillow", "youtube_search": "youtube-search"}
# Provided by the bot's own venv (pyproject dependencies), not by TOOL_DEPS.
BOT_PROVIDES = {"aiogram", "aiohttp", "jaato", "jaato_sdk", "jaato_client_telegram",
                "pydantic", "pydantic_settings", "yaml", "structlog", "websockets", "ddgs"}

FAKE_UV = """#!/usr/bin/env bash
echo "$*" >> "$UV_LOG"
"""
# python3 that records `-m venv` and runs everything else for real.
FAKE_PY = """#!/usr/bin/env bash
if [ "$1 $2" = "-m venv" ]; then echo "$*" >> "$VENV_LOG"; exit 0; fi
exec "$REAL_PY" "$@"
"""


def _tool(d, name, deps=None, body=""):
    decl = f"TOOL_DEPS = {deps!r}\n" if deps is not None else ""
    (d / f"{name}.py").write_text(f"{decl}{body}TOOL_SCHEMA = {{'name': '{name}'}}\n")


def _run(tmp_path, tools_venv_exists=True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, src in (("uv", FAKE_UV), ("py", FAKE_PY)):
        (bin_dir / name).write_text(src)
        (bin_dir / name).chmod(0o755)
    host_tools = tmp_path / "host_tools"
    host_tools.mkdir(exist_ok=True)
    tv = tmp_path / "ws" / ".jaato" / "tool-venv"
    if tools_venv_exists:
        (tv / "bin").mkdir(parents=True)
        (tv / "bin" / "python").write_text("#!/bin/sh\n")
        (tv / "bin" / "python").chmod(0o755)
    uv_log, venv_log = tmp_path / "uv.log", tmp_path / "venv.log"
    r = subprocess.run(
        ["bash", "-c", f'source "{DEPLOY}"; SYSTEMD_MODE=user; resolve_bot_paths; '
                       f'HOST_TOOLS_DIR="{host_tools}"; TOOLS_VENV="{tv}"; install_tool_deps'],
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "USER": "u",
             "PYTHON_BIN": str(bin_dir / "py"), "REAL_PY": sys.executable,
             "UV_LOG": str(uv_log), "VENV_LOG": str(venv_log)},
        capture_output=True, text=True)
    read = lambda p: p.read_text().splitlines() if p.exists() else []
    return r, read(uv_log), read(venv_log), tv, host_tools


# ── the deploy step ──────────────────────────────────────────────────────────

def test_installs_the_union_of_every_tools_deps_into_the_tool_venv(tmp_path):
    (tmp_path / "host_tools").mkdir()
    _tool(tmp_path / "host_tools", "a", ["numpy", "pillow"])
    _tool(tmp_path / "host_tools", "b", ["pillow", "httpx"])
    _tool(tmp_path / "host_tools", "c")                       # declares nothing
    r, uv, _, tv, _ = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert uv == [f"pip install --python {tv}/bin/python numpy pillow httpx"]


def test_no_declared_deps_installs_nothing(tmp_path):
    (tmp_path / "host_tools").mkdir()
    _tool(tmp_path / "host_tools", "a", [])
    r, uv, venv, _, _ = _run(tmp_path, tools_venv_exists=False)
    assert r.returncode == 0, r.stderr
    assert uv == [] and venv == []


def test_a_missing_tool_venv_is_created_the_way_the_framework_does(tmp_path):
    (tmp_path / "host_tools").mkdir()
    _tool(tmp_path / "host_tools", "a", ["numpy"])
    r, _, venv, tv, _ = _run(tmp_path, tools_venv_exists=False)
    assert r.returncode == 0, r.stderr
    assert venv == [f"-m venv --without-pip --system-site-packages {tv}"]


def test_an_unparsable_tool_stops_the_deploy_naming_it(tmp_path):
    (tmp_path / "host_tools").mkdir()
    (tmp_path / "host_tools" / "broken.py").write_text("def (:\n")
    r, uv, _, _, _ = _run(tmp_path)
    assert r.returncode != 0
    assert "broken.py" in r.stderr and uv == []


def test_the_tool_venv_matches_the_profile_and_the_bot_config():
    """Three places name the same venv; the deploy step must install where they read."""
    text = DEPLOY.read_text()
    assert 'TOOLS_VENV="$WORKSPACE/.jaato/tool-venv"' in text
    assert text.count('workspace_venv: ".jaato/tool-venv"') == 3
    assert 'host_tools_venv: "\\${JAATO_TG_WORKSPACE}/.jaato/tool-venv"' in text


# ── the declarations themselves ──────────────────────────────────────────────

def _third_party_imports(tree):
    std = set(sys.stdlib_module_names)
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            mods.add(n.module.split(".")[0])
    return {m for m in mods if m not in std and m not in BOT_PROVIDES}


def _declared(tree):
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "TOOL_DEPS" for t in n.targets):
            return set(ast.literal_eval(n.value))
    return set()


def test_every_example_tool_declares_what_it_imports():
    """Lazy imports count: youtube_search loads fine and fails on its first call."""
    missing = {}
    for f in sorted(EXAMPLES.glob("*.py")):
        tree = ast.parse(f.read_text())
        need = {DIST_FOR_IMPORT.get(m, m) for m in _third_party_imports(tree)}
        gap = need - _declared(tree)
        if gap:
            missing[f.name] = sorted(gap)
    assert not missing, f"undeclared PyPI deps (add TOOL_DEPS): {missing}"
