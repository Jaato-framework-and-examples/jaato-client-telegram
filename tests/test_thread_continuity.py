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
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            if "message_thread_id" in kwargs:  # first try (with injected thread) fails
                raise TelegramBadRequest(
                    method="x", message="Bad Request: message thread not found"
                )
            return "ok"

    bot = _FlakyBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: 555)
    assert await tb.send_message(chat_id=1, text="hi") == "ok"  # didn't raise
    assert len(bot.calls) == 2  # injected, then retried without
    assert "message_thread_id" not in bot.calls[1]


# ── delivered-content capture (already_shown_to_user) ────────────────────────
# A model-authored tool that renders to Telegram and returns only a status must
# still be visible to the model: make_executor records what ThreadAwareBot sent
# during execute() and folds it into the result under `already_shown_to_user`.


def _executor_for(execute_fn, chat_id=1, thread=None):
    """Wire a tool's execute() through a ThreadAwareBot(chat_id) like the bot does."""
    from jaato_client_telegram.host_tool_loader import make_executor

    bot = _RecBot()
    tbot = ThreadAwareBot(bot, chat_id=chat_id, thread_getter=lambda: thread)
    return make_executor(execute_fn, tbot, chat_id), bot


@pytest.mark.asyncio
async def test_delivered_content_folded_when_tool_returns_status_only():
    # The ephemerides failure mode: send the content, return just a status.
    async def execute(args, ctx):
        await ctx.bot.send_message(chat_id=ctx.chat_id, text="On this day: X happened.")
        return {"status": "sent"}

    executor, _ = _executor_for(execute)
    result = await executor({})
    assert result["status"] == "sent"
    fold = result["already_shown_to_user"]
    assert fold["content"] == ["On this day: X happened."]
    # leads with an imperative do-not-repeat directive the model reads as a value
    assert "do not" in fold["note"].lower() and "already" in fold["note"].lower()


@pytest.mark.asyncio
async def test_no_key_when_tool_returns_content_without_sending():
    # The weather tool shape: return content, send nothing directly → nothing to fold.
    async def execute(args, ctx):
        return {"result": "22°C, sunny"}

    executor, bot = _executor_for(execute)
    result = await executor({})
    assert result == {"result": "22°C, sunny"}
    assert "already_shown_to_user" not in result
    assert bot.calls == []


@pytest.mark.asyncio
async def test_capture_does_not_clobber_tool_supplied_key():
    async def execute(args, ctx):
        await ctx.bot.send_message(chat_id=ctx.chat_id, text="captured")
        return {"already_shown_to_user": ["author's own value"]}

    executor, _ = _executor_for(execute)
    result = await executor({})
    assert result["already_shown_to_user"] == ["author's own value"]  # setdefault, not clobbered


@pytest.mark.asyncio
async def test_captures_caption_for_media_and_multiple_sends():
    async def execute(args, ctx):
        await ctx.bot.send_message(chat_id=ctx.chat_id, text="line one")
        await ctx.bot.send_photo(ctx.chat_id, "file_id", caption="a caption")
        await ctx.bot.send_photo(ctx.chat_id, "file_id")  # no caption → nothing to record
        return {"status": "ok"}

    executor, _ = _executor_for(execute)
    result = await executor({})
    assert result["already_shown_to_user"]["content"] == ["line one", "a caption"]


@pytest.mark.asyncio
async def test_sends_to_other_chat_not_captured():
    async def execute(args, ctx):
        await ctx.bot.send_message(chat_id=999, text="different chat")  # not ctx.chat_id
        return {"status": "ok"}

    executor, _ = _executor_for(execute, chat_id=1)
    result = await executor({})
    assert "already_shown_to_user" not in result


@pytest.mark.asyncio
async def test_record_delivery_no_ops_without_active_recorder():
    # A built-in executor path (no make_executor recorder) must send fine and record nothing.
    bot = _RecBot()
    tb = ThreadAwareBot(bot, chat_id=1, thread_getter=lambda: None)
    assert await tb.send_message(chat_id=1, text="hi") == "ok"  # no crash, send happened
    assert bot.calls[0][0] == "send_message"


@pytest.mark.asyncio
async def test_concurrent_executors_do_not_cross_contaminate():
    import asyncio

    async def make_tool(tag):
        async def execute(args, ctx):
            await ctx.bot.send_message(chat_id=ctx.chat_id, text=f"content-{tag}")
            return {"status": "sent"}

        return execute

    exec_a, _ = _executor_for(await make_tool("A"), chat_id=1)
    exec_b, _ = _executor_for(await make_tool("B"), chat_id=2)
    ra, rb = await asyncio.gather(exec_a({}), exec_b({}))
    assert ra["already_shown_to_user"]["content"] == ["content-A"]
    assert rb["already_shown_to_user"]["content"] == ["content-B"]


@pytest.mark.asyncio
async def test_error_in_tool_returns_error_not_capture():
    async def execute(args, ctx):
        await ctx.bot.send_message(chat_id=ctx.chat_id, text="partial")
        raise RuntimeError("boom")

    executor, _ = _executor_for(execute)
    result = await executor({})
    assert result == {"error": "boom"}


# ── post-image fold (rendering.fold_post_image_text) ──────────────────────────
# After a host tool sends an image, the turn's trailing narration should fold (edit)
# into a placeholder dropped at the image's position, so it stays above a reply the
# user slips into the gap — instead of appending a new message below it.


def _fold_renderer(max_len=4096):
    from jaato_client_telegram.renderer import ResponseRenderer

    return ResponseRenderer(max_message_length=max_len, fold_post_image_text=True)


def _mock_initial(chat_id=1):
    from unittest.mock import AsyncMock, MagicMock

    m = MagicMock()
    m.chat.id = chat_id
    m.is_topic_message = False
    m.message_thread_id = None
    m.bot.send_chat_action = AsyncMock()

    def _make_msg(*a, **k):
        sm = MagicMock()
        sm.edit_text = AsyncMock()
        sm.delete = AsyncMock()
        return sm

    m.answer = AsyncMock(side_effect=_make_msg)
    return m


@pytest.mark.asyncio
async def test_open_fold_slot_creates_placeholder():
    from jaato_client_telegram.renderer import _FOLD_PLACEHOLDER

    r, ctx, initial = _fold_renderer(), _mk_ctx(), _mock_initial()
    await r._open_fold_slot(initial, ctx)
    assert ctx.fold_target is not None and ctx.fold_text == ""
    assert initial.answer.await_args.args[0] == _FOLD_PLACEHOLDER  # the "writing…" cue


@pytest.mark.asyncio
async def test_emit_one_folds_via_edit_not_new_message():
    r, ctx, initial = _fold_renderer(), _mk_ctx(), _mock_initial()
    ph = _mk_ph()
    ctx.fold_target = ph
    await r._emit_one(initial, ctx, "I have searched. Can you see it now?")
    ph.edit_text.assert_awaited()  # folded via edit
    assert "searched" in ctx.fold_text
    initial.answer.assert_not_awaited()  # no new bubble below


@pytest.mark.asyncio
async def test_emit_one_fold_accumulates_multiple_units():
    r, ctx, initial = _fold_renderer(), _mk_ctx(), _mock_initial()
    ph = _mk_ph()
    ctx.fold_target = ph
    await r._emit_one(initial, ctx, "first")
    await r._emit_one(initial, ctx, "second")
    assert "first" in ctx.fold_text and "second" in ctx.fold_text
    assert ph.edit_text.await_count == 2


@pytest.mark.asyncio
async def test_emit_one_fold_overflow_closes_and_sends_normally():
    r, ctx, initial = _fold_renderer(max_len=40), _mk_ctx(), _mock_initial()
    ph = _mk_ph()
    ctx.fold_target = ph
    ctx.fold_text = "x" * 38
    await r._emit_one(initial, ctx, "this pushes the fold well over the 40-char limit")
    assert ctx.fold_target is None  # fold closed on overflow
    ph.edit_text.assert_not_awaited()  # overflow unit not folded
    initial.answer.assert_awaited()  # sent normally instead


@pytest.mark.asyncio
async def test_open_fold_slot_deletes_prior_unused_placeholder():
    r, ctx, initial = _fold_renderer(), _mk_ctx(), _mock_initial()
    await r._open_fold_slot(initial, ctx)
    first = ctx.fold_target
    await r._open_fold_slot(initial, ctx)  # nothing folded into first → replace it
    first.delete.assert_awaited()
    assert ctx.fold_target is not first


def _mk_ctx():
    from jaato_client_telegram.renderer import StreamingContext

    return StreamingContext()


def _mk_ph():
    from unittest.mock import AsyncMock, MagicMock

    ph = MagicMock()
    ph.edit_text = AsyncMock()
    ph.delete = AsyncMock()
    return ph


@pytest.mark.asyncio
async def test_send_photo_triggers_open_fold_slot():
    from jaato_client_telegram.host_tool_loader import register_fold_hook, unregister_fold_hook

    called = []

    async def hook():
        called.append("opened")

    register_fold_hook(1, hook)
    try:
        tb = ThreadAwareBot(_RecBot(), chat_id=1, thread_getter=lambda: None)
        await tb.send_photo(1, "file_id")
        assert called == ["opened"]
    finally:
        unregister_fold_hook(1)


@pytest.mark.asyncio
async def test_send_message_and_other_chat_do_not_open_fold():
    from jaato_client_telegram.host_tool_loader import register_fold_hook, unregister_fold_hook

    called = []

    async def hook():
        called.append("x")

    register_fold_hook(1, hook)
    try:
        tb = ThreadAwareBot(_RecBot(), chat_id=1, thread_getter=lambda: None)
        await tb.send_message(chat_id=1, text="hi")  # not a photo → no fold
        await tb.send_photo(2, "file_id")  # other chat → no fold
        assert called == []
    finally:
        unregister_fold_hook(1)


@pytest.mark.asyncio
async def test_send_photo_noops_without_fold_hook():
    # Flag off ⇒ renderer never registers a fold hook ⇒ open_fold_slot no-ops and the
    # image send still succeeds unchanged.
    tb = ThreadAwareBot(_RecBot(), chat_id=1, thread_getter=lambda: None)
    assert await tb.send_photo(1, "file_id") == "ok"


@pytest.mark.asyncio
async def test_emit_one_whitespace_unit_keeps_fold_open():
    # A whitespace-only segment must NOT close the slot (else the real narration that
    # follows sends as its own bubble below a reply — the bug the feature fixes).
    r, ctx, initial = _fold_renderer(), _mk_ctx(), _mock_initial()
    ph = _mk_ph()
    ctx.fold_target = ph
    await r._emit_one(initial, ctx, "   \n  ")
    assert ctx.fold_target is ph  # slot still open
    ph.edit_text.assert_not_awaited()
    initial.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_fold_deletes_unused_placeholder():
    # The mid-turn-injection / turn-end close path must delete an unused placeholder,
    # not orphan a stray "✍️ …" bubble.
    r, ctx = _fold_renderer(), _mk_ctx()
    ph = _mk_ph()
    ctx.fold_target = ph  # fold_text == "" → unused
    await r._discard_fold_if_unused(ctx)
    ph.delete.assert_awaited()
    assert ctx.fold_target is None


@pytest.mark.asyncio
async def test_discard_fold_keeps_used_placeholder_but_closes_slot():
    r, ctx = _fold_renderer(), _mk_ctx()
    ph = _mk_ph()
    ctx.fold_target = ph
    ctx.fold_text = "already folded narration"
    await r._discard_fold_if_unused(ctx)
    ph.delete.assert_not_awaited()  # real content stays in place
    assert ctx.fold_target is None  # but the slot is closed


@pytest.mark.asyncio
async def test_turn_completed_closes_fold_so_next_turn_does_not_bleed():
    # The cross-turn bug: the ctx persists across turns (resets only at AGENT_COMPLETED),
    # so an image turn's placeholder must be closed at TURN_COMPLETED(stop) — else the
    # NEXT turn's reply folds into it. Drive two turns through the real stream loop.
    from unittest.mock import AsyncMock, MagicMock
    from jaato_client_telegram.renderer import ResponseRenderer, _FOLD_PLACEHOLDER
    from jaato_client_telegram.host_tool_loader import open_fold_slot

    sent = []

    def _mk(*a, **k):
        m = MagicMock()
        m.edit_text = AsyncMock()
        m.delete = AsyncMock()
        m._init_text = a[0] if a else k.get("text")
        sent.append(m)
        return m

    msg = MagicMock()
    msg.chat.id = 1
    msg.is_topic_message = False
    msg.message_thread_id = None
    msg.answer = AsyncMock(side_effect=_mk)
    msg.bot.send_chat_action = AsyncMock()
    msg.bot.send_message = AsyncMock(side_effect=_mk)

    class Ev:
        def __init__(self, **k):
            self.__dict__.update(k)

    async def gen():
        yield Ev(type="agent.output", source="model", mode="write", text="Here is an image.\n\n")
        await open_fold_slot(1)  # a host tool sent an image → placeholder opens
        yield Ev(type="agent.output", source="model", mode="write", text="I hope it displays.\n\n")
        yield Ev(type="turn.completed", finish_reason="stop")  # turn A ends here
        yield Ev(type="agent.output", source="model", mode="write", text="Excellent, glad it worked!\n\n")
        yield Ev(type="agent.completed")

    await ResponseRenderer(fold_post_image_text=True).stream_response(
        msg, gen(), thread_id_getter=lambda: None
    )

    ph = next(m for m in sent if m._init_text == _FOLD_PLACEHOLDER)
    folded = " ".join(str(c.args[0]) for c in ph.edit_text.await_args_list)
    assert "displays" in folded  # turn A's narration folded into the placeholder
    assert "Excellent" not in folded  # turn B did NOT bleed into it (fold closed at TURN_COMPLETED)
    ph.delete.assert_not_awaited()  # placeholder had content → kept, not orphaned


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
        def __init__(self, **k):
            self.__dict__.update(k)

    async def gen():
        for e in (
            {
                "type": "agent.output",
                "source": "model",
                "mode": "write",
                "text": "Starting a fresh topic over here, nice and long enough.",
            },
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
    await _stream_one(msg, thread_getter=lambda: 555)  # 555 invalid
    msg.bot.send_message.assert_awaited()  # tried the thread
    msg.answer.assert_awaited()  # fell back to plain answer()
