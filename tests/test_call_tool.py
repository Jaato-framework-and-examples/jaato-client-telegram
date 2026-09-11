"""ctx.call_tool() — host tools composing other host tools.

A host tool can invoke a peer host tool by name via ``ctx.call_tool()``, against a
registry the bot assembles per session (built-ins + installed tools). These pin the
``ToolContext.call_tool`` contract — dispatch, required-arg validation, error
surfacing, and the recursion guard — without a real bot/session.
"""

import asyncio

from jaato_client_telegram.host_tool_loader import MAX_CALL_DEPTH, ToolContext


def _registry(entries: dict) -> dict:
    """{name: (handler, schema)} -> the {name: {"handler","schema"}} registry shape."""
    return {n: {"handler": h, "schema": s} for n, (h, s) in entries.items()}


def test_call_tool_composes_and_returns_result():
    async def echo(args):
        return {"echoed": args.get("x")}

    reg = _registry({"echo": (echo, {"name": "echo", "parameters": {"required": ["x"]}})})
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    assert asyncio.run(ctx.call_tool("echo", {"x": 42})) == {"echoed": 42}


def test_call_tool_not_wired_when_no_registry():
    ctx = ToolContext(bot=None, chat_id=1)  # no tool_registry
    assert "not available" in asyncio.run(ctx.call_tool("x"))["error"]


def test_call_tool_unknown_lists_available():
    async def a(args):
        return {}

    reg = _registry({"a": (a, {"name": "a", "parameters": {}})})
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    err = asyncio.run(ctx.call_tool("missing"))["error"]
    assert "unknown host tool" in err and "a" in err


def test_call_tool_missing_required_arg():
    async def f(args):
        return {"ok": True}

    reg = _registry({"f": (f, {"name": "f", "parameters": {"required": ["y"]}})})
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    assert "missing required" in asyncio.run(ctx.call_tool("f", {}))["error"]


def test_call_tool_wraps_non_dict_result():
    async def scalar(args):
        return "hello"

    reg = _registry({"s": (scalar, {"name": "s", "parameters": {}})})
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    assert asyncio.run(ctx.call_tool("s"))["result"] == "hello"


def test_call_tool_surfaces_callee_error():
    async def boom(args):
        raise RuntimeError("kaboom")

    reg = _registry({"boom": (boom, {"name": "boom", "parameters": {}})})
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    err = asyncio.run(ctx.call_tool("boom"))["error"]
    assert "boom failed" in err and "kaboom" in err


def test_call_tool_composition_two_tools():
    async def inner(args):
        return {"n": args["n"] * 2}

    async def wrap(args):
        r = await ctx.call_tool("inner", {"n": args["n"]})
        return {"wrapped": r["n"] + 1}

    reg = {
        "inner": {"handler": inner, "schema": {"name": "inner", "parameters": {"required": ["n"]}}},
        "wrap": {"handler": wrap, "schema": {"name": "wrap", "parameters": {"required": ["n"]}}},
    }
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    assert asyncio.run(ctx.call_tool("wrap", {"n": 5}))["wrapped"] == 11


def test_call_tool_recursion_guard_stops_a_cycle():
    reg: dict = {}

    async def recur(args):
        return await ctx.call_tool("recur")

    reg["recur"] = {"handler": recur, "schema": {"name": "recur", "parameters": {}}}
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    err = asyncio.run(ctx.call_tool("recur"))["error"]
    assert "depth limit" in err and str(MAX_CALL_DEPTH) in err


def test_call_tool_depth_resets_between_top_level_calls():
    # After a guarded cycle, a fresh top-level call must start at depth 0 again.
    reg: dict = {}

    async def recur(args):
        return await ctx.call_tool("recur")

    async def ok(args):
        return {"ok": True}

    reg["recur"] = {"handler": recur, "schema": {"name": "recur", "parameters": {}}}
    reg["ok"] = {"handler": ok, "schema": {"name": "ok", "parameters": {}}}
    ctx = ToolContext(bot=None, chat_id=1, tool_registry=reg)
    asyncio.run(ctx.call_tool("recur"))  # exhausts the guard
    assert asyncio.run(ctx.call_tool("ok")) == {"ok": True}
