"""Out-of-band tool sends flush the renderer's pending narration FIRST, so a tool's
message/result lands AFTER the narration (not before it — the renderer's throttled
narration otherwise loses the race to the tool's direct send). The flush is
centralized in ThreadAwareBot: the single choke point every host-tool send funnels
through (send_to_telegram / show_image / ask_user / any dynamic ctx.bot.send_*)."""

import asyncio

from jaato_client_telegram import host_tool_loader as htl
from jaato_client_telegram.host_tool_loader import register_flush_hook, unregister_flush_hook
from jaato_client_telegram.thread_bot import ThreadAwareBot


class _RawBot:
    def __init__(self, order):
        self._order = order

    async def send_message(self, chat_id=None, text=None, **kw):
        self._order.append(("send_message", chat_id))

    async def send_photo(self, chat_id=None, **kw):
        self._order.append(("send_photo", chat_id))

    async def get_me(self):  # a NON-send method must not be wrapped / must not flush
        self._order.append("get_me")
        return "me"


def test_threadawarebot_flushes_before_every_send_to_its_chat():
    async def run():
        order: list = []

        async def flush():
            order.append("flush")

        register_flush_hook(42, flush)
        try:
            tbot = ThreadAwareBot(_RawBot(order), 42, lambda: None)
            await tbot.send_message(chat_id=42, text="the tool's result card")
            assert order == ["flush", ("send_message", 42)]   # flush STRICTLY before the send
            order.clear()
            await tbot.send_photo(chat_id=42)
            assert order == ["flush", ("send_photo", 42)]      # every send_* flushes
        finally:
            unregister_flush_hook(42)
    asyncio.run(run())


def test_no_flush_for_other_chat_or_nonsend_method():
    async def run():
        order: list = []

        async def flush():
            order.append("flush")

        register_flush_hook(42, flush)
        try:
            tbot = ThreadAwareBot(_RawBot(order), 42, lambda: None)
            await tbot.send_message(chat_id=99, text="different chat")
            assert order == [("send_message", 99)]   # other chat ⇒ don't flush THIS chat
            order.clear()
            await tbot.get_me()                       # non-send ⇒ not wrapped, no flush
            assert order == ["get_me"]
        finally:
            unregister_flush_hook(42)
    asyncio.run(run())


def test_flush_before_prompt_noop_and_error_safe():
    async def run():
        await htl.flush_before_prompt(123)            # no hook registered ⇒ no-op

        async def boom():
            raise RuntimeError("renderer gone")

        register_flush_hook(7, boom)
        try:
            await htl.flush_before_prompt(7)          # a faulting hook must NOT propagate
        finally:
            unregister_flush_hook(7)
    asyncio.run(run())


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
