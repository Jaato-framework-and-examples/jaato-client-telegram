"""Dynamic-tool dependency venv: the bot prepends the per-workspace tools venv's
site-packages to sys.path so an in-process host tool can import a dependency the
confined runner installed there (notebook/cli/shell `pip install`).
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

from jaato_client_telegram.host_tool_loader import (
    load_all_tools,
    tools_venv_site_packages,
)
from jaato_client_telegram.session_pool import SessionPool


def _pool_with_venv(venv: str) -> SessionPool:
    pool = SessionPool.__new__(SessionPool)  # bypass __init__ (needs real config)
    pool._ws_config = SimpleNamespace(host_tools_venv=venv)
    return pool


def test_site_packages_matches_bot_interpreter(tmp_path):
    venv = tmp_path / ".jaato" / "tool-venv"
    sp = tools_venv_site_packages(venv)
    assert sp == venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def test_wire_is_noop_when_unconfigured():
    before = list(sys.path)
    _pool_with_venv("")._wire_tools_venv()
    assert sys.path == before


def test_wire_prepends_even_if_venv_absent(tmp_path):
    venv = tmp_path / "tool-venv"          # does NOT exist yet
    sp = str(tools_venv_site_packages(venv))
    try:
        _pool_with_venv(str(venv))._wire_tools_venv()
        assert sys.path[0] == sp           # prepended so a later install resolves
        # idempotent: a second wire doesn't duplicate
        _pool_with_venv(str(venv))._wire_tools_venv()
        assert sys.path.count(sp) == 1
    finally:
        while sp in sys.path:
            sys.path.remove(sp)


def test_host_tool_imports_dep_installed_into_the_venv(tmp_path):
    """End-to-end: a package that appears in the tools venv AFTER wiring becomes
    importable by a host tool once load_all_tools invalidate_caches()es."""
    venv = tmp_path / "tool-venv"
    site = tools_venv_site_packages(venv)
    sp = str(site)
    host_tools = tmp_path / "host_tools"
    host_tools.mkdir()
    # A host tool that imports a third-party dep only present in the tools venv.
    (host_tools / "needs_dep.py").write_text(
        "import faketooldep_xyz\n"
        'TOOL_SCHEMA = {"name": "needs_dep", "description": "d", '
        '"parameters": {"type": "object", "properties": {}}}\n'
        "async def execute(args, ctx):\n    return {'result': faketooldep_xyz.VALUE}\n"
    )
    try:
        # Wire the (still-empty) venv onto sys.path — mirrors bot startup.
        _pool_with_venv(str(venv))._wire_tools_venv()
        # Tool can't load yet: the dep isn't installed.
        assert "needs_dep" not in load_all_tools(host_tools)
        # The confined runner "pip installs" the dep into the venv.
        site.mkdir(parents=True)
        (site / "faketooldep_xyz.py").write_text("VALUE = 42\n")
        # Now a (re)load resolves it — load_all_tools invalidates finder caches.
        tools = load_all_tools(host_tools)
        assert "needs_dep" in tools
    finally:
        while sp in sys.path:
            sys.path.remove(sp)
        sys.modules.pop("faketooldep_xyz", None)
        importlib.invalidate_caches()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
