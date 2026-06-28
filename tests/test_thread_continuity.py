"""Tests for per-chat Telegram thread continuity (ChatThreadStore + ThreadAwareBot)."""

import json

import pytest

from jaato_client_telegram.thread_store import ChatThreadStore
from jaato_client_telegram.thread_bot import ThreadAwareBot


# ── ChatThreadStore ──────────────────────────────────────────────────────────

def test_sync_inbound_sets_current_and_known():
    s = ChatThreadStore()
    assert s.current(1) is None
    s.sync_inbound(1, 42)
    assert s.current(1) == 42
    s.sync_inbound(1, None)  # user back in the main view
    assert s.current(1) is None


def test_distinct_across_chats():
    s = ChatThreadStore()
    s.sync_inbound(1, 7)
    s.sync_inbound(2, 99)
    assert s.current(1) == 7 and s.current(2) == 99


def test_persistence_round_trip(tmp_path):
    p = str(tmp_path / "threads.json")
    s = ChatThreadStore(p)
    s.sync_inbound(1, 10)
    cur = s.current(1)
    # reload from disk
    s2 = ChatThreadStore(p)
    assert s2.current(1) == cur
    on_disk = json.loads((tmp_path / "threads.json").read_text())
    assert "1" in on_disk and "known" in on_disk["1"] and 10 in on_disk["1"]["known"]


def test_inmemory_when_no_path():
    s = ChatThreadStore("")  # empty path => no persistence, no error
    s.sync_inbound(1, 3)
    assert s.current(1) == 3


# ── ThreadAwareBot ───────────────────────────────────────────────────────────

class _RecBot:
    def __init__(self):
        self.calls = []
        self.id = 12345  # a non-send attribute passes through

    async def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return "ok"

    async def send_photo(self, chat_id, photo, **kwargs):
        self.calls.append(("send_photo", {"chat_id": chat_id, **kwargs}))
        return "ok"


@pytest.mark.asyncio
async def test_injects_thread_for_matching_chat():
    bot = _RecBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: 77)
    await tb.send_message(chat_id=1, text="hi")
    assert bot.calls[0][1]["message_thread_id"] == 77


@pytest.mark.asyncio
async def test_does_not_override_explicit_thread():
    bot = _RecBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: 77)
    await tb.send_message(chat_id=1, text="hi", message_thread_id=9)
    assert bot.calls[0][1]["message_thread_id"] == 9


@pytest.mark.asyncio
async def test_no_inject_for_other_chat():
    bot = _RecBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: 77)
    await tb.send_message(chat_id=2, text="hi")
    assert "message_thread_id" not in bot.calls[0][1]


@pytest.mark.asyncio
async def test_no_inject_when_thread_none():
    bot = _RecBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: None)
    await tb.send_message(chat_id=1, text="hi")
    assert "message_thread_id" not in bot.calls[0][1]


@pytest.mark.asyncio
async def test_inject_works_on_positional_chat_id():
    bot = _RecBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: 5)
    await tb.send_photo(1, "file_id")  # chat_id positional
    assert bot.calls[0][1]["message_thread_id"] == 5


def test_non_send_attribute_passes_through():
    bot = _RecBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: 5)
    assert tb.id == 12345


@pytest.mark.asyncio
async def test_proxy_retries_without_thread_on_thread_not_found():
    from aiogram.exceptions import TelegramBadRequest

    class _FlakyBot:
        def __init__(self): self.calls = []
        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            if "message_thread_id" in kwargs:   # first try (with injected thread) fails
                raise TelegramBadRequest(method="x", message="Bad Request: message thread not found")
            return "ok"

    bot = _FlakyBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: 555)
    assert await tb.send_message(chat_id=1, text="hi") == "ok"   # didn't raise
    assert len(bot.calls) == 2                                    # injected, then retried without
    assert "message_thread_id" not in bot.calls[1]


# ── renderer follows the store's current thread (+ stale-thread guard) ────────

def _msg_mock(inbound_thread):
    from unittest.mock import AsyncMock, MagicMock
    m = MagicMock()
    m.chat.id = 1
    m.message_thread_id = inbound_thread
    m.is_topic_message = inbound_thread is not None
    m.answer = AsyncMock(return_value=MagicMock())
    m.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    m.bot.send_chat_action = AsyncMock()
    return m


async def _stream_one(msg, thread_getter):
    from jaato_client_telegram.renderer import ResponseRenderer

    class Ev:
        def __init__(self, **k): self.__dict__.update(k)

    async def gen():
        for e in (
            {"type": "agent.output", "source": "model", "mode": "write",
             "text": "Starting a fresh topic over here, nice and long enough."},
            {"type": "agent.completed"},
        ):
            yield Ev(**e)

    await ResponseRenderer().stream_response(msg, gen(), thread_id_getter=thread_getter)


@pytest.mark.asyncio
async def test_renderer_sends_explicitly_when_thread_differs_from_inbound():
    # When the store's current thread differs from the inbound message's thread,
    # the renderer sends via bot.send_message(message_thread_id=…) (Message.answer()
    # can't be told the thread).
    msg = _msg_mock(inbound_thread=100)
    await _stream_one(msg, thread_getter=lambda: 555)
    assert msg.bot.send_message.await_count >= 1
    assert msg.bot.send_message.call_args.kwargs.get("message_thread_id") == 555
    msg.answer.assert_not_called()


@pytest.mark.asyncio
async def test_renderer_keeps_answer_when_thread_matches_inbound():
    # current thread == inbound → answer() already follows it; no override, no
    # behaviour change (this is the common path that keeps existing tests green).
    msg = _msg_mock(inbound_thread=100)
    await _stream_one(msg, thread_getter=lambda: 100)
    msg.answer.assert_awaited()
    assert msg.bot.send_message.await_count == 0  # text didn't go via send_message


@pytest.mark.asyncio
async def test_renderer_recovers_from_invalid_thread_instead_of_crashing():
    # A stale/invalid thread id (the bot can't create threads in a private chat)
    # must NOT crash the turn — the renderer drops the thread and delivers.
    from aiogram.exceptions import TelegramBadRequest
    msg = _msg_mock(inbound_thread=100)
    msg.bot.send_message = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(
        side_effect=TelegramBadRequest(method="x", message="Bad Request: message thread not found")
    )
    await _stream_one(msg, thread_getter=lambda: 555)   # 555 invalid
    msg.bot.send_message.assert_awaited()               # tried the thread
    msg.answer.assert_awaited()                          # fell back to plain answer()
