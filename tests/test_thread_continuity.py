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


def test_open_new_mints_distinct_id():
    s = ChatThreadStore()
    s.sync_inbound(1, 5)
    a = s.open_new(1)
    assert a != 5 and a == s.current(1)
    b = s.open_new(1)
    assert b not in (5, a)  # distinct from every id seen
    assert s.current(1) == b


def test_distinct_across_chats():
    s = ChatThreadStore()
    s.sync_inbound(1, 7)
    s.sync_inbound(2, 99)
    assert s.current(1) == 7 and s.current(2) == 99


def test_persistence_round_trip(tmp_path):
    p = str(tmp_path / "threads.json")
    s = ChatThreadStore(p)
    s.sync_inbound(1, 10)
    s.open_new(1)
    cur = s.current(1)
    # reload from disk
    s2 = ChatThreadStore(p)
    assert s2.current(1) == cur
    # known persisted -> next mint still distinct
    assert s2.open_new(1) != cur
    on_disk = json.loads((tmp_path / "threads.json").read_text())
    assert "1" in on_disk and "known" in on_disk["1"]


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


# ── open_thread executor ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_thread_adopts_root_message_id_as_current():
    from unittest.mock import MagicMock, AsyncMock
    from jaato_client_telegram.session_pool import SessionPool

    bot = MagicMock()
    sent = MagicMock(); sent.message_id = 4321
    bot.send_message = AsyncMock(return_value=sent)
    pool = SessionPool(ws_config=MagicMock(), bot=bot, file_config=None, session_store_path="")

    res = await pool._make_open_thread_executor(chat_id=7)({"title": "Groceries"})
    assert res["status"] == "ok" and res["thread_id"] == 4321
    assert pool.current_thread(7) == 4321
    # the root title goes via the RAW bot with NO message_thread_id (must not thread
    # into the OLD thread)
    bot.send_message.assert_awaited_once()
    assert "message_thread_id" not in bot.send_message.call_args.kwargs


@pytest.mark.asyncio
async def test_open_thread_requires_title():
    from unittest.mock import MagicMock
    from jaato_client_telegram.session_pool import SessionPool

    pool = SessionPool(ws_config=MagicMock(), bot=MagicMock(), file_config=None, session_store_path="")
    res = await pool._make_open_thread_executor(7)({"title": "  "})
    assert "error" in res


# ── renderer follows the current thread (open_thread override) ────────────────

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
async def test_renderer_overrides_thread_after_open_thread():
    # current thread (555) != the inbound message's thread (100) → open_thread
    # branched, so the model narration must go via bot.send_message(thread=555),
    # NOT Message.answer() (which would stick to the inbound thread).
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
