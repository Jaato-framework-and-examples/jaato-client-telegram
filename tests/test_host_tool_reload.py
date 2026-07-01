"""A re-registered (modified) dynamic tool must load its NEW code immediately,
even when overwritten + reloaded within the same second (the register_tool path).

Regression: load_tool_file used importlib's __pycache__, whose bytecode cache is
validated by source mtime at 1-second granularity → a same-second reload ran
stale bytecode and the new version only kicked in on a new session.
"""

import asyncio

import pytest

from jaato_client_telegram.host_tool_loader import load_tool_file


def _write(path, marker):
    path.write_text(
        'TOOL_SCHEMA = {"name": "mytool", "description": "d", '
        '"parameters": {"type": "object", "properties": {}}}\n'
        f'async def execute(args, ctx):\n    return {{"result": "{marker}"}}\n'
    )


def _call(execute):
    return asyncio.new_event_loop().run_until_complete(execute({}, None))["result"]


def test_reload_reflects_new_code_same_second(tmp_path):
    p = tmp_path / "mytool.py"
    _write(p, "V1")
    _, e1 = load_tool_file(p)
    assert _call(e1) == "V1"

    # Overwrite + reload immediately (same second — the register_tool timing).
    _write(p, "V2")
    _, e2 = load_tool_file(p)
    assert _call(e2) == "V2"        # was "V1" before the fix (stale .pyc)

    _write(p, "V3")
    _, e3 = load_tool_file(p)
    assert _call(e3) == "V3"


def test_no_pyc_written_for_tool(tmp_path):
    # compile()+exec() must not create a __pycache__ for the tool file.
    p = tmp_path / "mytool.py"
    _write(p, "V1")
    load_tool_file(p)
    assert not (tmp_path / "__pycache__").exists()


def test_load_errors_surface_as_valueerror(tmp_path):
    p = tmp_path / "broken.py"
    p.write_text("this is not valid python :::\n")
    with pytest.raises(ValueError):
        load_tool_file(p)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
