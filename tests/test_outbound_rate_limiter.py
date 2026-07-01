"""Outbound rate limiter: proactive per-chat pacing + reactive 429 retry."""

import asyncio
import time

import pytest
from aiogram.exceptions import TelegramRetryAfter

import jaato_client_telegram.outbound_rate_limiter as rlmod
from jaato_client_telegram.outbound_rate_limiter import OutboundRateLimiter, _is_message_send


class _M:
    """Minimal stand-in for an aiogram method object."""
    def __init__(self, api, chat_id=None):
        self.__api_method__ = api
        if chat_id is not None:
            self.chat_id = chat_id


def test_classification():
    assert _is_message_send(_M("sendMessage"))
    assert _is_message_send(_M("sendPhoto"))
    assert _is_message_send(_M("forwardMessage"))
    assert _is_message_send(_M("copyMessage"))
    # edits (streaming) + chat actions (typing) + non-chat methods are NOT paced
    assert not _is_message_send(_M("editMessageText"))
    assert not _is_message_send(_M("sendChatAction"))
    assert not _is_message_send(_M("answerCallbackQuery"))


@pytest.mark.asyncio
async def test_paces_same_chat_message_sends():
    rl = OutboundRateLimiter(per_chat_interval=0.2, global_min_interval=0)
    times = []

    async def make_request(bot, method):
        times.append(time.monotonic())
        return "ok"

    for _ in range(3):
        await rl(make_request, None, _M("sendMessage", chat_id=1))
    assert times[1] - times[0] >= 0.18          # spaced by ~the per-chat interval
    assert times[2] - times[1] >= 0.18


@pytest.mark.asyncio
async def test_different_chats_not_serialized_by_per_chat_gap():
    rl = OutboundRateLimiter(per_chat_interval=0.5, global_min_interval=0)
    times = []

    async def make_request(bot, method):
        times.append(time.monotonic())
        return "ok"

    await rl(make_request, None, _M("sendMessage", chat_id=1))
    await rl(make_request, None, _M("sendMessage", chat_id=2))   # other chat: no per-chat wait
    assert times[1] - times[0] < 0.2


@pytest.mark.asyncio
async def test_edits_not_paced():
    rl = OutboundRateLimiter(per_chat_interval=0.5, global_min_interval=0)
    times = []

    async def make_request(bot, method):
        times.append(time.monotonic())
        return "ok"

    for _ in range(3):
        await rl(make_request, None, _M("editMessageText", chat_id=1))
    assert times[-1] - times[0] < 0.2           # no pacing for edits


@pytest.mark.asyncio
async def test_retries_on_429(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(rlmod.asyncio, "sleep", fake_sleep)
    rl = OutboundRateLimiter(per_chat_interval=0, global_min_interval=0)
    calls = {"n": 0}

    async def make_request(bot, method):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=3)
        return "ok"

    res = await rl(make_request, None, _M("sendMessage", chat_id=1))
    assert res == "ok"
    assert calls["n"] == 2                        # retried once
    assert slept and slept[-1] >= 3               # honored retry_after


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
