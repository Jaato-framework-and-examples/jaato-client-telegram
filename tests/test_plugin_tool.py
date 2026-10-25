"""Host tools reaching framework plugin tools (issue #47): ToolContext.invoke_plugin_tool
→ SessionPool.invoke_plugin_tool_command → WSRecoveryClient.invoke_plugin_tool
(protocol 1.37, jaato #1606).

The daemon half (session-less plugin host, pass:// resolution) lives in jaato-server;
here we verify the bot's wiring: the call's shape, the result mapping, the clear
errors below the protocol floor / on a failed dial, and the default permission-ASK
routing to the chat's user.
"""

import asyncio
from types import SimpleNamespace

import jaato_client_telegram.host_tool_loader as loader
from jaato_client_telegram.host_tool_loader import ToolContext
from jaato_client_telegram.session_pool import SessionPool

# ---- ToolContext surface ---------------------------------------------------


def _ctx_with(answer, calls=None):
    async def plugin_tool_fn(plugin, tool, args, **kw):
        if calls is not None:
            calls.append((plugin, tool, args, kw))
        return answer

    return ToolContext(bot=None, chat_id=7, plugin_tool_fn=plugin_tool_fn)


def test_ctx_unwired_returns_error():
    r = asyncio.run(ToolContext(bot=None, chat_id=7).invoke_plugin_tool("p", "t"))
    assert r["category"] == "disabled" and "error" in r


def test_ctx_success_passes_call_through():
    calls: list = []
    ctx = _ctx_with({"ok": True, "success": True, "result": {"status": 200}}, calls)
    r = asyncio.run(
        ctx.invoke_plugin_tool(
            "service_connector", "call_service", {"service": "github"}, profile="ops", timeout=60.0
        )
    )
    assert r == {"result": {"status": 200}}
    plugin, tool, args, kw = calls[0]
    assert (plugin, tool, args) == ("service_connector", "call_service", {"service": "github"})
    assert kw["profile"] == "ops" and kw["plugin_configs"] is None
    assert kw["timeout"] == 60.0
    assert callable(kw["on_permission"])  # default ASK routing is always wired


def test_ctx_not_run_and_tool_failure_are_errors():
    r = asyncio.run(
        _ctx_with(
            {"ok": False, "category": "not_in_surface", "error": "profile does not enable it"}
        ).invoke_plugin_tool("p", "t")
    )
    assert r["category"] == "not_in_surface" and r["error"] == "profile does not enable it"

    r = asyncio.run(
        _ctx_with({"ok": True, "success": False, "result": "denied"}).invoke_plugin_tool("p", "t")
    )
    assert r["category"] == "tool_failed" and r["result"] == "denied"


def test_ctx_explicit_on_permission_is_passed_through():
    calls: list = []

    def deny(_event):
        return "n"

    ctx = _ctx_with({"ok": True, "success": True, "result": 1}, calls)
    asyncio.run(ctx.invoke_plugin_tool("p", "t", on_permission=deny))
    assert calls[0][3]["on_permission"] is deny


def test_default_ask_maps_chosen_label_to_key(monkeypatch):
    seen = {}

    async def fake_ask_user(bot, chat_id, text, options, timeout=300.0):
        seen.update(chat_id=chat_id, text=text, options=options, timeout=timeout)
        return "Allow once"

    monkeypatch.setattr(loader, "ask_user", fake_ask_user)
    event = SimpleNamespace(
        tool_name="call_service",
        response_options=[{"key": "y", "label": "Allow once"}, {"key": "n", "label": "Deny"}],
        prompt_lines=["GET <api.github.com>"],
        warnings=None,
    )
    ctx = ToolContext(bot=None, chat_id=9)
    key = asyncio.run(ctx._ask_user_permission(event, 42.0))
    assert key == "y"
    assert seen["chat_id"] == 9 and seen["options"] == ["Allow once", "Deny"]
    assert seen["timeout"] == 42.0
    assert "&lt;api.github.com&gt;" in seen["text"]  # HTML-escaped for parse_mode=HTML


def test_default_ask_timeout_denies(monkeypatch):
    async def no_answer(*_a, **_k):
        return None

    monkeypatch.setattr(loader, "ask_user", no_answer)
    event = SimpleNamespace(tool_name="t", response_options=[], prompt_lines=None, warnings=None)
    assert asyncio.run(ToolContext(bot=None, chat_id=1)._ask_user_permission(event, 1.0)) is None


# ---- pool command (short-lived session-less client) ------------------------


class _FakeClient:
    def __init__(self, *, connects=True, raises=None, answer=None):
        self.connects = connects
        self.raises = raises
        self.answer = answer
        self.calls: list = []
        self.closed = False

    async def connect(self):
        return self.connects

    async def invoke_plugin_tool(self, plugin, tool, args=None, **kw):
        self.calls.append((plugin, tool, args, kw))
        if self.raises is not None:
            raise self.raises
        return self.answer

    async def close(self):
        self.closed = True


def _pool(fc):
    return SimpleNamespace(
        _make_client=lambda chat_id=None: fc, _ws_config=SimpleNamespace(url="ws://daemon")
    )


def test_pool_command_maps_result_and_closes_client():
    fc = _FakeClient(
        answer=SimpleNamespace(ok=True, category="", error="", success=True, result={"x": 1})
    )
    r = asyncio.run(
        SessionPool.invoke_plugin_tool_command(
            _pool(fc), "service_connector", "call_service", {"a": 1}, profile="ops"
        )
    )
    assert r == {"ok": True, "category": "", "error": "", "success": True, "result": {"x": 1}}
    assert fc.calls[0][:3] == ("service_connector", "call_service", {"a": 1})
    assert fc.calls[0][3]["profile"] == "ops"
    assert fc.closed


def test_pool_command_below_protocol_floor_is_clear_error():
    fc = _FakeClient(raises=ValueError("invoke_plugin_tool: this daemon speaks 1.36"))
    r = asyncio.run(SessionPool.invoke_plugin_tool_command(_pool(fc), "p", "t"))
    assert r["ok"] is False and r["category"] == "unsupported_protocol"
    assert "1.36" in r["error"]
    assert fc.closed


def test_pool_command_failed_dial():
    fc = _FakeClient(connects=False)
    r = asyncio.run(SessionPool.invoke_plugin_tool_command(_pool(fc), "p", "t"))
    assert r["ok"] is False and r["category"] == "not_connected"
    assert fc.calls == []


def test_pool_command_old_sdk():
    class _OldClient:
        pass

    r = asyncio.run(SessionPool.invoke_plugin_tool_command(_pool(_OldClient()), "p", "t"))
    assert r["ok"] is False and r["category"] == "unsupported_sdk"


def test_pool_command_timeout():
    fc = _FakeClient(raises=asyncio.TimeoutError())
    r = asyncio.run(SessionPool.invoke_plugin_tool_command(_pool(fc), "p", "t", timeout=5))
    assert r["category"] == "timeout"
