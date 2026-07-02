"""ChatPump: intra-turn steering (a mid-turn message is delivered into the LIVE
turn, not deferred to a new one) + turn serialization, ordering, error recovery.
"""

import asyncio
from types import SimpleNamespace

from jaato_client_telegram.chat_pump import ChatPump, PumpItem


# ---- fakes -----------------------------------------------------------------

class _Ctx:
    def __init__(self, stalled=False):
        self.stalled = stalled


class FakePool:
    def __init__(self):
        self.sent: list[tuple] = []          # (session_id, text, attachments)
        self.sessions: dict[int, str] = {}
        self.reattach: dict[int, bool] = {}
        self.welcomed: set[int] = set()
        self.forgotten: list[int] = []
        self.threads: dict[int, object] = {}
        self.send_fail_once = False

    def sync_thread(self, chat_id, tid):
        self.threads[chat_id] = tid

    def get_session_info(self, chat_id):
        return self.sessions.get(chat_id)

    async def get_or_create_session(self, chat_id):
        sid = self.sessions.get(chat_id) or f"sess-{chat_id}"
        self.sessions[chat_id] = sid
        return sid

    def took_reattach(self, chat_id):
        return self.reattach.get(chat_id, False)

    def claim_first_contact(self, chat_id):
        if chat_id in self.welcomed:
            return False
        self.welcomed.add(chat_id)
        return True

    async def send_message(self, session_id, text, attachments=None):
        if self.send_fail_once:
            self.send_fail_once = False
            raise RuntimeError("boom")
        self.sent.append((session_id, text, attachments))

    async def events(self, session_id):
        return iter(())

    def current_thread(self, chat_id):
        return self.threads.get(chat_id)

    async def forget_session(self, chat_id):
        self.forgotten.append(chat_id)


class FakeRenderer:
    def __init__(self):
        self.started: list[int] = []
        self.completed: list[int] = []
        self.release: dict[int, asyncio.Event] = {}
        self.ctx: dict[int, _Ctx] = {}

    def _ev(self, idx):
        return self.release.setdefault(idx, asyncio.Event())

    async def stream_response(self, initial_message, event_stream, thread_id_getter=None):
        idx = len(self.started)
        self.started.append(idx)
        await self._ev(idx).wait()          # block the turn until the test releases it
        self.completed.append(idx)
        return self.ctx.get(idx, _Ctx(stalled=False))


class FakeBot:
    async def send_chat_action(self, chat_id, action):
        pass


class FakeMessage:
    def __init__(self, chat_id, text=""):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_thread_id = None
        self.text = text
        self.bot = FakeBot()
        self.answers: list[str] = []
        self.replies: list[str] = []

    async def answer(self, text, parse_mode=None):
        self.answers.append(text)

    async def reply(self, text, parse_mode=None):
        self.replies.append(text)


async def _until(cond, timeout=2.0):
    for _ in range(int(timeout / 0.002)):
        if cond():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition not met in time")


def _item(chat_id, text, **kw):
    return PumpItem(chat_id=chat_id, message=FakeMessage(chat_id, text), text=text, **kw)


# ---- tests -----------------------------------------------------------------

def test_new_turn_delivers_and_renders():
    async def run():
        pool, rend = FakePool(), FakeRenderer()
        pump = ChatPump(pool, rend)
        pump.submit(_item(1, "hello", apply_welcome=False))
        await _until(lambda: rend.started == [0] and len(pool.sent) == 1)
        assert pool.sent[0][1] == "hello"
        rend._ev(0).set()
        await _until(lambda: rend.completed == [0])
        await pump.shutdown()
    asyncio.run(run())


def test_mid_turn_message_is_steered_not_a_new_turn():
    """THE core behavior: a message that arrives while a turn streams is delivered
    to the live session (send_message) — NOT rendered as a second turn."""
    async def run():
        pool, rend = FakePool(), FakeRenderer()
        pump = ChatPump(pool, rend)

        pump.submit(_item(1, "A", apply_welcome=False))
        # turn A is streaming (render 0 blocked), first send done
        await _until(lambda: rend.started == [0] and len(pool.sent) == 1)

        # B arrives mid-turn
        pump.submit(_item(1, "B", apply_welcome=False))
        await _until(lambda: len(pool.sent) == 2)

        assert pool.sent[1][1] == "B"          # B was delivered to the session…
        assert rend.started == [0]             # …and NO second stream_response spawned

        rend._ev(0).set()                      # end the (single) turn
        await _until(lambda: rend.completed == [0])
        await asyncio.sleep(0.01)
        assert rend.started == [0]             # still just one render, ever
        await pump.shutdown()
    asyncio.run(run())


def test_next_message_after_completion_starts_a_fresh_turn():
    async def run():
        pool, rend = FakePool(), FakeRenderer()
        pump = ChatPump(pool, rend)

        pump.submit(_item(1, "A", apply_welcome=False))
        await _until(lambda: rend.started == [0] and len(pool.sent) == 1)
        rend._ev(0).set()
        await _until(lambda: rend.completed == [0])
        await asyncio.sleep(0.01)              # let the actor park between turns

        pump.submit(_item(1, "B", apply_welcome=False))
        await _until(lambda: rend.started == [0, 1] and len(pool.sent) == 2)
        assert pool.sent[1][1] == "B"          # B is a NEW turn, its own render
        rend._ev(1).set()
        await pump.shutdown()
    asyncio.run(run())


def test_error_notifies_and_actor_survives():
    async def run():
        pool, rend = FakePool(), FakeRenderer()
        pool.send_fail_once = True
        pump = ChatPump(pool, rend)

        bad = _item(1, "A", apply_welcome=False)
        pump.submit(bad)
        # send_message raises → _handle_error notifies, no render happens
        await _until(lambda: any("Error" in a or "issue" in a for a in bad.message.answers))
        assert rend.started == []

        # actor recovered: a later message works
        pump.submit(_item(1, "B", apply_welcome=False))
        await _until(lambda: rend.started == [0] and pool.sent and pool.sent[-1][1] == "B")
        rend._ev(0).set()
        await pump.shutdown()
    asyncio.run(run())


def test_stall_forgets_session():
    async def run():
        pool, rend = FakePool(), FakeRenderer()
        rend.ctx[0] = _Ctx(stalled=True)
        pump = ChatPump(pool, rend)
        item = _item(1, "A", apply_welcome=False)
        pump.submit(item)
        await _until(lambda: rend.started == [0])
        rend._ev(0).set()
        await _until(lambda: pool.forgotten == [1])
        assert any("stopped responding" in a for a in item.message.answers)
        await pump.shutdown()
    asyncio.run(run())


def test_welcome_prefix_applied_on_first_contact():
    async def run():
        from jaato_client_telegram.welcome_store import WELCOME_PREFIX
        pool, rend = FakePool(), FakeRenderer()
        pump = ChatPump(pool, rend)
        pump.submit(_item(1, "hi", apply_welcome=True))
        await _until(lambda: len(pool.sent) == 1)
        assert pool.sent[0][1] == WELCOME_PREFIX + "hi"
        rend._ev(0).set()
        await pump.shutdown()
    asyncio.run(run())


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
