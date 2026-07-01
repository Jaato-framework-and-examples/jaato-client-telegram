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


def test_executor_sees_dep_installed_after_creation(tmp_path):
    """make_executor invalidate_caches() lets a tool import a dep that appears in
    a sys.path dir AFTER the executor was built — the install-then-use-now case
    (e.g. moon_phase importing skyfield inside execute() right after a pip
    install into the workspace venv)."""
    import asyncio

    from jaato_client_telegram.host_tool_loader import make_executor

    venv = tmp_path / "tool-venv"
    site = tools_venv_site_packages(venv)
    sp = str(site)
    sys.path.insert(0, sp)
    try:
        importlib.invalidate_caches()
        try:                                  # force a scan that caches the dir empty
            import faketooldep_late  # noqa: F401
        except ModuleNotFoundError:
            pass

        async def execute(args, ctx):
            import faketooldep_late            # imported at CALL time
            return {"result": faketooldep_late.V}

        executor = make_executor(execute, bot=None, chat_id=1)
        site.mkdir(parents=True)              # runner "installs" the dep AFTER
        (site / "faketooldep_late.py").write_text("V = 7\n")

        out = asyncio.new_event_loop().run_until_complete(executor({}))
        assert out == {"result": 7}
    finally:
        while sp in sys.path:
            sys.path.remove(sp)
        sys.modules.pop("faketooldep_late", None)
        importlib.invalidate_caches()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
