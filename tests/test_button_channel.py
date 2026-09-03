"""ctx.buttons() interactive-keyboard channel + ctx.ask_multi() convenience.

The bot owns the plumbing: a tool renders a keyboard, taps arrive over the main
bot's single poll (simulated here by calling resolve_button_tap directly, as the
callback_query router does), the tool redraws in place, and the session cleans up
its keyboard on exit. These tests pin that round-trip without a real Telegram.
"""

import asyncio
from types import SimpleNamespace

from jaato_client_telegram.host_tool_loader import (
    _ASK_MULTI_DONE,
    _BUTTON_CHANNELS,
    Tap,
    ToolContext,
    resolve_button_tap,
)


class FakeBot:
    """Records send/edit calls and signals each render so a test can drive taps
    deterministically (feed one tap, wait for the redraw, feed the next)."""

    def __init__(self) -> None:
        self.sent: list = []
        self.edits: list = []
        self.render = asyncio.Event()
        self._mid = 100

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self._mid += 1
        self.sent.append((text, reply_markup))
        self.render.set()
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
        self.edits.append(("markup", message_id, reply_markup))
        self.render.set()
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kw):
        self.edits.append(("text", message_id, text, reply_markup))
        self.render.set()
        return SimpleNamespace(message_id=message_id)


async def _wait_render(bot: FakeBot) -> None:
    await asyncio.wait_for(bot.render.wait(), timeout=2)
    bot.render.clear()


def _tap(chat_id: int, value: str) -> bool:
    """Simulate a user tapping the button whose value is `value` on the one live
    channel — exactly what the callback_query router does via resolve_button_tap."""
    req_id, channel = next(iter(_BUTTON_CHANNELS.items()))
    index = [v for v, _ in channel.values].index(value)
    return resolve_button_tap(f"btn:{req_id}:{index}", chat_id)


# ---- resolve_button_tap routing -------------------------------------------------


def test_tap_routes_value_and_label_onto_queue():
    async def run():
        bot = FakeBot()
        session = ToolContext(bot=bot, chat_id=5).buttons()
        async with session as ui:
            await ui.send("pick", [[("Xavi", "x"), ("Joan", "j")]])
            assert _tap(5, "j") is True
            tap = await asyncio.wait_for(session._channel.queue.get(), timeout=1)
            assert isinstance(tap, Tap)
            assert (tap.value, tap.label, tap.index) == ("j", "Joan", 1)

    asyncio.run(run())


def test_tap_wrong_chat_is_rejected():
    async def run():
        bot = FakeBot()
        session = ToolContext(bot=bot, chat_id=5).buttons()
        async with session as ui:
            await ui.send("pick", [[("Xavi", "x")]])
            # A tap attributed to a different chat must never drive this channel.
            req_id = session._channel.req_id
            assert resolve_button_tap(f"btn:{req_id}:0", 999) is False
            assert session._channel.queue.empty()

    asyncio.run(run())


def test_tap_bad_data_and_expired_channel():
    # No live channels registered here.
    assert resolve_button_tap("perm:abc:0", 5) is False  # wrong prefix
    assert resolve_button_tap("btn:nope", 5) is False  # unparseable
    assert resolve_button_tap("btn:ghost:0", 5) is False  # unknown req_id


def test_session_cleanup_strips_keyboard_and_deregisters():
    async def run():
        bot = FakeBot()
        session = ToolContext(bot=bot, chat_id=5).buttons()
        async with session as ui:
            await ui.send("pick", [[("A", "a")]])
            req_id = session._channel.req_id
            assert req_id in _BUTTON_CHANNELS
        # On exit: keyboard stripped (reply_markup=None) + channel deregistered.
        assert req_id not in _BUTTON_CHANNELS
        assert bot.edits[-1] == ("markup", 101, None)

    asyncio.run(run())


# ---- ask_multi convenience -----------------------------------------------------


def test_ask_multi_toggle_returns_selection_in_option_order():
    async def run():
        bot = FakeBot()
        ctx = ToolContext(bot=bot, chat_id=5)
        task = asyncio.create_task(ctx.ask_multi("Available?", ["A", "B", "C"], timeout=5))
        await _wait_render(bot)  # initial keyboard
        _tap(5, "A")
        await _wait_render(bot)  # tick A
        _tap(5, "B")
        await _wait_render(bot)  # tick B
        _tap(5, "A")
        await _wait_render(bot)  # untick A
        _tap(5, _ASK_MULTI_DONE)  # done -> loop breaks
        result = await asyncio.wait_for(task, timeout=2)
        assert result == ["B"]  # option order, A removed

    asyncio.run(run())


def test_ask_multi_times_out_to_current_selection():
    async def run():
        bot = FakeBot()
        ctx = ToolContext(bot=bot, chat_id=5)
        # Tiny timeout, never tap Done: returns whatever was ticked when it expires.
        task = asyncio.create_task(ctx.ask_multi("Available?", ["A", "B"], timeout=0.05))
        await _wait_render(bot)
        _tap(5, "B")
        await _wait_render(bot)
        result = await asyncio.wait_for(task, timeout=2)
        assert result == ["B"]

    asyncio.run(run())
