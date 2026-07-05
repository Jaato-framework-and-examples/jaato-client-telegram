"""ask_user() flushes the renderer's pending narration BEFORE it sends its prompt,
so an out-of-band tool prompt lands after the narration (not before it)."""

import asyncio

from jaato_client_telegram import host_tool_loader as htl
from jaato_client_telegram.host_tool_loader import (
    ask_user,
    register_flush_hook,
    unregister_flush_hook,
)


class _FakeBot:
    def __init__(self, order):
        self._order = order

    async def send_message(self, chat_id, text, reply_markup=None):
        self._order.append("send")


async def _tap_after_send():
    """Resolve the pending ask future once ask_user has sent (so it returns)."""
    for _ in range(20):
        await asyncio.sleep(0)
        for fut in list(htl._PENDING_ASKS.values()):
            if not fut.done():
                fut.set_result(0)
                return


def test_ask_user_flushes_before_send():
    async def run():
        order: list[str] = []

        async def flush():
            order.append("flush")

        register_flush_hook(42, flush)
        try:
            tap = asyncio.create_task(_tap_after_send())
            choice = await ask_user(_FakeBot(order), 42, "pick one", ["a", "b"], timeout=5)
            await tap
            assert order == ["flush", "send"]   # flush strictly BEFORE the prompt send
            assert choice == "a"
        finally:
            unregister_flush_hook(42)
    asyncio.run(run())


def test_ask_user_without_hook_still_sends():
    async def run():
        order: list[str] = []
        tap = asyncio.create_task(_tap_after_send())
        choice = await ask_user(_FakeBot(order), 99, "pick", ["x"], timeout=5)
        await tap
        assert order == ["send"]                 # no hook registered ⇒ just sends
        assert choice == "x"
    asyncio.run(run())


def test_flush_before_prompt_swallows_hook_errors():
    async def run():
        async def boom():
            raise RuntimeError("renderer gone")

        register_flush_hook(7, boom)
        try:
            await htl.flush_before_prompt(7)     # must NOT raise — prompt must still send
        finally:
            unregister_flush_hook(7)
    asyncio.run(run())


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
