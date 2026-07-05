"""The per-session wake watcher (SessionPool): one long-lived event stream (opened
eagerly via client.open_event_stream(), jaato-sdk #524) that detects the wake
user-echo marker and mode-flips the SAME stream into the renderer, discarding
non-wake events. See docs/design/pr-review-feedback-loop.md."""

import asyncio
from types import SimpleNamespace

from jaato_sdk.plugins.model_provider.types import UNTRUSTED_OPEN, wrap_untrusted_content

from jaato_client_telegram.session_pool import _is_wake_echo, SessionPool


# ---- the detector -----------------------------------------------------------

def test_is_wake_echo_matches_only_wake_source():
    wake = SimpleNamespace(source="user", text=wrap_untrusted_content("review!", source="wake:github-pr"))
    assert _is_wake_echo(wake) is True
    # other untrusted content (a tool result) uses the SAME open marker, source=<tool>
    web = SimpleNamespace(source="user", text=wrap_untrusted_content("page", source="web_fetch"))
    assert _is_wake_echo(web) is False
    # a plain user message is not wrapped at all
    assert _is_wake_echo(SimpleNamespace(source="user", text="hello")) is False
    # model output is never the trigger
    assert _is_wake_echo(SimpleNamespace(source="model", text=UNTRUSTED_OPEN + " source=wake:x⟧")) is False
    # non-AGENT_OUTPUT events (no text) don't blow up
    assert _is_wake_echo(SimpleNamespace(kind="status")) is False


# ---- the watcher: mode-flip + discard + survival ----------------------------

class _FakeStream:
    """Fake _SyncSubscribedStream: async iterator over a queue; ``None`` sentinel →
    StopAsyncIteration. aclose() marks closed (idempotent)."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        ev = await self.q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self):
        self._stream = _FakeStream()

    def open_event_stream(self):
        return self._stream  # eager subscribe modelled by returning the live stream


class _FakeRenderer:
    def __init__(self, stop_on):
        self.calls = []            # (initial_message, event_stream)
        self.rendered = []         # events consumed while rendering
        self._stop_on = stop_on

    async def stream_response(self, *, initial_message, event_stream, thread_id_getter):
        self.calls.append((initial_message, event_stream))
        async for ev in event_stream:  # consumes the SAME stream, post-marker
            if getattr(ev, "source", None) == self._stop_on:
                return                 # break-not-close: leaves the stream open
            self.rendered.append(ev)


def _bare_pool(renderer):
    pool = SessionPool.__new__(SessionPool)  # bypass __init__
    pool._renderer = renderer
    pool._bot = SimpleNamespace(send_message=lambda *a, **k: None)
    pool._watchers = {}
    pool._thread_store = SimpleNamespace(current=lambda cid: None)
    return pool


def _wake(n=""):
    return SimpleNamespace(source="user",
                           text=wrap_untrusted_content(f"fix {n}", source="wake:github-pr"))


async def _drain(pool, chat_id):
    for _ in range(40):
        await asyncio.sleep(0)
        if pool._watchers[chat_id].done():
            return


def test_wake_renders_on_same_stream_and_discards_nonwake():
    async def run():
        client = _FakeClient()
        stream = client._stream
        renderer = _FakeRenderer(stop_on="__done__")
        pool = _bare_pool(renderer)

        pool._start_wake_watcher(chat_id=7, client=client)   # sync: opens stream + spawns task
        assert 7 in pool._watchers

        model = SimpleNamespace(source="model", text="on it")
        done = SimpleNamespace(source="__done__", text="")
        nonwake = SimpleNamespace(source="user", text="ordinary turn echo")
        for ev in (_wake(), model, done, nonwake, None):
            stream.q.put_nowait(ev)

        await _drain(pool, 7)
        assert len(renderer.calls) == 1
        assert renderer.calls[0][1] is stream          # mode-flip on the SAME stream
        assert renderer.rendered == [model]            # model output rendered
        assert nonwake not in renderer.rendered        # non-wake discarded
        await pool._stop_wake_watcher(7)
    asyncio.run(run())


def test_watcher_survives_a_second_wake_on_the_same_stream():
    """stream_response BREAKs at completion (not aclose) — else the watcher's next
    iteration ends and it dies after one wake. Two wakes on ONE stream prove it."""
    async def run():
        client = _FakeClient()
        stream = client._stream
        renderer = _FakeRenderer(stop_on="__done__")
        pool = _bare_pool(renderer)
        pool._start_wake_watcher(chat_id=7, client=client)

        m1 = SimpleNamespace(source="model", text="one")
        m2 = SimpleNamespace(source="model", text="two")
        done = SimpleNamespace(source="__done__", text="")
        for ev in (_wake(1), m1, done, _wake(2), m2, done, None):
            stream.q.put_nowait(ev)

        await _drain(pool, 7)
        assert len(renderer.calls) == 2                # BOTH wakes rendered on one stream
        assert renderer.rendered == [m1, m2]
        await pool._stop_wake_watcher(7)
    asyncio.run(run())


def test_watcher_acloses_stream_on_stop():
    async def run():
        client = _FakeClient()
        renderer = _FakeRenderer(stop_on="__done__")
        pool = _bare_pool(renderer)
        pool._start_wake_watcher(chat_id=7, client=client)
        await asyncio.sleep(0)
        await pool._stop_wake_watcher(7)               # cancels → finally aclose()
        assert client._stream.closed is True           # unsubscribed on teardown
    asyncio.run(run())


def test_start_wake_watcher_noop_without_renderer():
    pool = _bare_pool(renderer=None)
    pool._renderer = None
    pool._start_wake_watcher(chat_id=1, client=_FakeClient())
    assert pool._watchers == {}   # feature off ⇒ no watcher


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
