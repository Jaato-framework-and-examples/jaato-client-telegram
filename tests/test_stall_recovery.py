"""A pending permission/clarification prompt must NOT trip the stall reset."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import jaato_client_telegram.renderer as r


class _Ev:
    def __init__(self, **k): self.__dict__.update(k)


def _msg():
    m = MagicMock()
    m.chat.id = 1
    m.answer = AsyncMock(return_value=MagicMock())
    m.bot.send_chat_action = AsyncMock()
    return m


async def _slow_completed():
    await asyncio.sleep(0.2)          # slower than the (patched) 0.05s stall timeout
    yield _Ev(type="agent.completed")


@pytest.mark.asyncio
async def test_pending_prompt_suppresses_false_stall(monkeypatch):
    monkeypatch.setattr(r, "_STALL_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(r, "_AWAIT_USER_TIMEOUT_SECS", 5.0)
    perm = MagicMock(); perm.get_pending.return_value = object()   # a prompt is pending
    ctx = await r.ResponseRenderer(permission_handler=perm).stream_response(_msg(), _slow_completed())
    assert ctx.stalled is False        # awaiting the user → no false reset


@pytest.mark.asyncio
async def test_no_pending_prompt_still_stalls(monkeypatch):
    monkeypatch.setattr(r, "_STALL_TIMEOUT_SECS", 0.05)
    perm = MagicMock(); perm.get_pending.return_value = None       # nothing pending
    ctx = await r.ResponseRenderer(permission_handler=perm).stream_response(_msg(), _slow_completed())
    assert ctx.stalled is True         # genuine silence → recover as before


async def _revive_then_answer():
    # Cold revive: an INIT_PROGRESS, then a silent plugin-bootstrap gap LONGER than
    # the (patched) mid-turn stall timeout but within the revive cap, then the answer.
    yield _Ev(type="init.progress", message="Loading plugins")
    await asyncio.sleep(0.15)
    yield _Ev(type="agent.output", source="model", mode="write", text="green")
    yield _Ev(type="agent.completed")


@pytest.mark.asyncio
async def test_cold_revive_does_not_false_stall(monkeypatch):
    monkeypatch.setattr(r, "_STALL_TIMEOUT_SECS", 0.05)   # would stall a 0.15s gap…
    monkeypatch.setattr(r, "_INIT_TIMEOUT_SECS", 5.0)     # …but the revive cap holds
    perm = MagicMock(); perm.get_pending.return_value = None
    ctx = await r.ResponseRenderer(permission_handler=perm).stream_response(_msg(), _revive_then_answer())
    assert ctx.stalled is False        # INIT_PROGRESS seen → revive cap covers the bootstrap gap
