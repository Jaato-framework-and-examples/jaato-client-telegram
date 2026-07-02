"""Per-chat message pump — intra-turn user steering.

Every message for a chat flows through ONE long-lived actor task that owns the
turn lifecycle. A message that arrives WHILE a turn is streaming is delivered to
the live session immediately (``send_message`` → the server enqueues it as a
USER-priority prompt and injects it between tool calls, per
``jaato-server/server/core.py`` ``send_message``'s ``_model_running`` branch), and
the SAME running ``stream_response`` renders the steered continuation. When the
agent completes, the next queued message starts a fresh turn.

This REPLACES the old per-chat ``asyncio.Lock`` that was held across the whole
``stream_response`` — which blocked every mid-turn message until the turn ended,
so steering never landed until the narrative was already over (the bug this
fixes). The lock's job (serialize a chat's messages, never two turns at once) is
now done by the queue + single actor; mid-turn messages are delivered rather than
deferred.

Key model facts this relies on (verified in the SDK/server):
- ``SessionPool.events()`` is a persistent fan-out subscription, so a mid-turn
  injection's continuation events reach the ``stream_response`` already iterating.
- ``stream_response`` returns on AGENT_COMPLETED (not TURN_COMPLETED), and a
  mid-turn USER inject keeps the agent running, so ONE ``stream_response`` spans
  the original turn plus every steered continuation until the agent truly stops.

The pump key is the jaato SESSION key = ``message.chat.id`` (private and group
alike — group sessions are created on ``chat.id``), so one actor == one session
== one turn stream.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jaato_client_telegram.welcome_store import WELCOME_PREFIX

if TYPE_CHECKING:
    from aiogram.types import Message

    from jaato_client_telegram.renderer import ResponseRenderer
    from jaato_client_telegram.session_pool import SessionPool

logger = logging.getLogger(__name__)


@dataclass
class PumpItem:
    """One inbound message to deliver to a chat's session."""

    chat_id: int
    message: "Message"           # tg message: rendering target + thread source
    text: str
    attachments: list | None = None
    apply_welcome: bool = False  # prepend the first-contact welcome (private only)
    reply: bool = False          # feedback via message.reply (group) vs answer


class ChatPump:
    """One inbox queue + one actor task per chat_id."""

    def __init__(self, pool: "SessionPool", renderer: "ResponseRenderer") -> None:
        self._pool = pool
        self._renderer = renderer
        self._inbox: dict[int, asyncio.Queue] = {}
        self._actors: dict[int, asyncio.Task] = {}

    def submit(self, item: PumpItem) -> None:
        """Enqueue a message and ensure the chat's actor is running.

        Non-blocking by design: it returns immediately so a mid-turn message is
        NOT gated behind the in-flight turn — that is the whole point. The actor
        (single consumer) preserves per-chat ordering and one-turn-at-a-time."""
        q = self._inbox.get(item.chat_id)
        if q is None:
            q = asyncio.Queue()
            self._inbox[item.chat_id] = q
        q.put_nowait(item)
        task = self._actors.get(item.chat_id)
        if task is None or task.done():
            self._actors[item.chat_id] = asyncio.create_task(self._actor(item.chat_id))

    async def shutdown(self) -> None:
        """Cancel all actor tasks (bot shutdown). In-flight turns are aborted."""
        tasks = list(self._actors.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:  # noqa: BLE001 — best-effort teardown
                pass
        self._actors.clear()

    async def _actor(self, chat_id: int) -> None:
        inbox = self._inbox[chat_id]
        get_task: asyncio.Task | None = None
        render: asyncio.Task | None = None
        try:
            while True:
                # `get_task` may be carried over from the previous turn's drain
                # loop (a pending inbox.get()) — don't lose it / its message.
                if get_task is None:
                    get_task = asyncio.create_task(inbox.get())
                item = await get_task
                get_task = None

                try:
                    # ---- start a NEW turn with `item` ----
                    session_id, text = await self._prepare_turn(item)
                    await self._pool.send_message(
                        session_id, text, attachments=item.attachments
                    )
                    render = asyncio.create_task(self._render(item, session_id))

                    # Drain the inbox WHILE the turn streams: each message that
                    # arrives mid-turn is injected into the LIVE turn (steering);
                    # the running `render` shows the continuation.
                    get_task = asyncio.create_task(inbox.get())
                    while True:
                        await asyncio.wait(
                            {render, get_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if render.done():
                            break  # turn ended; carry the pending get_task
                        mid = get_task.result()
                        get_task = asyncio.create_task(inbox.get())
                        await self._deliver_mid_turn(mid, session_id)

                    ctx = render.result()
                    render = None
                    await self._post_turn(item, ctx)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — turn boundary
                    render = None
                    await self._handle_error(item, e)
        except asyncio.CancelledError:
            pass
        finally:
            if get_task is not None and not get_task.done():
                get_task.cancel()
            if render is not None and not render.done():
                render.cancel()

    # ---- per-turn steps ----------------------------------------------------

    async def _prepare_turn(self, item: PumpItem) -> tuple[str, str]:
        """Session setup + feedback for a turn-STARTING message. Returns
        (session_id, text-to-send). Mirrors the old handlers' pre-send flow."""
        chat_id = item.chat_id
        self._pool.sync_thread(chat_id, item.message.message_thread_id)
        notify = item.message.reply if item.reply else item.message.answer

        is_first = self._pool.get_session_info(chat_id) is None
        if is_first:
            await notify(
                "⏳ Connecting to your session...\n"
                "(First message takes a few seconds to initialize)",
                parse_mode=None,
            )
        else:
            try:
                await item.message.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:  # noqa: BLE001 — typing is best-effort
                pass

        session_id = await self._pool.get_or_create_session(chat_id)
        if self._pool.took_reattach(chat_id):
            await notify("⏳ Resuming your previous conversation…", parse_mode=None)

        text = item.text
        if item.apply_welcome and self._pool.claim_first_contact(chat_id):
            text = WELCOME_PREFIX + text
        return session_id, text

    async def _deliver_mid_turn(self, item: PumpItem, session_id: str) -> None:
        """Inject a message into a LIVE turn. The server treats a send during an
        active turn as a USER-priority mid-turn prompt (steer). Attachments are
        passed through, but the server's inject path is text-only today, so a
        mid-turn image's bytes are not ferried — steering is text."""
        self._pool.sync_thread(item.chat_id, item.message.message_thread_id)
        text = item.text
        if item.apply_welcome and self._pool.claim_first_contact(item.chat_id):
            text = WELCOME_PREFIX + text
        await self._pool.send_message(session_id, text, attachments=item.attachments)

    async def _render(self, item: PumpItem, session_id: str):
        return await self._renderer.stream_response(
            initial_message=item.message,
            event_stream=await self._pool.events(session_id),
            thread_id_getter=lambda cid=item.chat_id: self._pool.current_thread(cid),
        )

    async def _post_turn(self, item: PumpItem, ctx) -> None:
        if ctx is not None and getattr(ctx, "stalled", False):
            notify = item.message.reply if item.reply else item.message.answer
            await notify(
                "⚠️ The session stopped responding — I've reset it. "
                "Please resend your message.",
                parse_mode=None,
            )
            await self._pool.forget_session(item.chat_id)

    async def _handle_error(self, item: PumpItem, e: Exception) -> None:
        logger.exception("pump: error handling message for chat %s", item.chat_id)
        notify = item.message.reply if item.reply else item.message.answer
        is_session_error = any(
            k in str(e).lower()
            for k in ("session", "connection", "disconnected", "timeout")
        )
        if is_session_error:
            text = (
                f"❌ Connection or session issue detected.\n\n"
                f"Details: {e}\n\n"
                f"Please send your message again to retry with a fresh session."
            )
        else:
            text = (
                f"❌ Error processing your message.\n\n"
                f"Details: {e}\n\n"
                f"Use /reset to start a fresh session if the problem persists."
            )
        if len(text) > 4096:
            text = text[:4000] + "\n\n... (truncated)"
        try:
            await notify(text, parse_mode=None)
        except Exception:  # noqa: BLE001
            logger.exception("pump: failed to send error notice to chat %s", item.chat_id)
