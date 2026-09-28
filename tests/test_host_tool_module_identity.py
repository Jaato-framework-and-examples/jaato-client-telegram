"""A host tool's module-level state must survive a reload that changed nothing.

Regression (the "one daily reminder fired six times" incident): ``load_all_tools``
recompiled every tool on every call, and it is called on EVERY session creation
and re-registration. A tool holding background work got a fresh module — and so a
fresh, empty bookkeeping dict — per call, while the ``asyncio`` tasks the previous
module created stayed alive on the loop. The reminder tool's own guard
(``if not _reminders``) therefore read an always-empty dict and armed another copy
of the schedule each time. Six live timers, one reminder, six wakes at 07:00.

The contract is content-keyed, not time-keyed: an UNCHANGED file keeps its module,
a CHANGED file reloads immediately (including a same-second overwrite, which is the
``register_tool`` path) and the outgoing module is offered ``on_unload()`` first.
"""

import asyncio

from jaato_client_telegram import host_tool_loader
from jaato_client_telegram.host_tool_loader import load_all_tools

STATEFUL = '''
TOOL_SCHEMA = {"name": "%s", "description": "d",
               "parameters": {"type": "object", "properties": {}}}
loads = 0
unloaded = []
loads += 1
async def execute(args, ctx):
    return {"result": "%s"}
def on_unload():
    unloaded.append(True)
'''


def _write(path, name="stateful", marker="V1"):
    path.write_text(STATEFUL % (name, marker))


def _fresh_cache():
    host_tool_loader._TOOL_CACHE.clear()


def _globals(tools, name="stateful"):
    return tools[name]["execute"].__globals__


def test_unchanged_file_keeps_its_module(tmp_path):
    """The whole point: module-level state must not reset on every call."""
    _fresh_cache()
    _write(tmp_path / "stateful.py")

    first = load_all_tools(tmp_path)
    g = _globals(first)
    g["armed"] = ["a timer"]          # stand-in for a live asyncio task

    second = load_all_tools(tmp_path)
    # Same module object -> the tool can SEE what it already armed.
    assert _globals(second) is g
    assert _globals(second).get("armed") == ["a timer"]
    assert _globals(second)["loads"] == 1      # compiled once, not twice


def test_changed_file_reloads_and_releases_the_old_module(tmp_path):
    _fresh_cache()
    p = tmp_path / "stateful.py"
    _write(p, marker="V1")
    first = load_all_tools(tmp_path)
    old_globals = _globals(first)

    _write(p, marker="V2")            # same second, as register_tool does
    second = load_all_tools(tmp_path)

    assert _globals(second) is not old_globals
    assert asyncio.run(second["stateful"]["execute"]({}, None))["result"] == "V2"
    # the outgoing module was told, so it can cancel its background work
    assert old_globals["unloaded"] == [True]


def test_removed_file_is_released(tmp_path):
    _fresh_cache()
    p = tmp_path / "stateful.py"
    _write(p)
    first = load_all_tools(tmp_path)
    g = _globals(first)

    p.unlink()
    assert load_all_tools(tmp_path) == {}
    assert g["unloaded"] == [True]


def test_a_failed_load_is_not_cached(tmp_path):
    """A tool whose dependency arrives a moment later must be retried."""
    _fresh_cache()
    p = tmp_path / "stateful.py"
    p.write_text("import a_package_that_does_not_exist_yet\n")
    assert load_all_tools(tmp_path) == {}

    _write(p)                          # "the dep landed" / the file was fixed
    tools = load_all_tools(tmp_path)
    assert "stateful" in tools
