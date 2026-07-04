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
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from jaato_client_telegram.welcome_store import WELCOME_PREFIX

if TYPE_CHECKING:
    from aiogram.types import Message

    from jaato_client_telegram.renderer import ResponseRenderer
    from jaato_client_telegram.session_pool import SessionPool

logger = logging.getLogger(__name__)

# A Telegram "typing…" chat action lasts ~5s; re-send a bit under that to keep it
# continuous during a slow turn.
_TYPING_INTERVAL = 4.0


@dataclass
class PumpItem:
    """One inbound message to deliver to a chat's session."""

    chat_id: int
    message: "Message"           # tg message (or OutboundAnchor): render target + thread
    text: str
    attachments: list | None = None
    apply_welcome: bool = False  # prepend the first-contact welcome (private only)
    reply: bool = False          # feedback via message.reply (group) vs answer
    # A DEFERRED item never steers a running turn: if a turn is active when it
    # arrives, it waits and runs as its OWN turn after the current one finishes.
    # Used for agent-facing events (e.g. a reminder firing) that must not barge
    # into a user's in-flight turn. A normal user message (defer=False) is
    # delivered mid-turn as a steer, per the pump's whole point.
    defer: bool = False


class OutboundAnchor:
    """A minimal Message-like object so a turn can be rendered with NO inbound
    Telegram message (e.g. a reminder waking the model). Implements only the
    surface stream_response / _safe_answer touch: chat.id, bot, message_thread_id,
    is_topic_message, answer, reply, answer_document. Sends via bot.send_message so
    every send still funnels through the outbound rate limiter."""

    is_topic_message = False

    def __init__(self, bot: Any, chat_id: int, thread_id: int | None = None) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_thread_id = thread_id

    async def answer(self, text: str, **kwargs):
        kwargs.pop("message_thread_id", None)
        return await self.bot.send_message(
            chat_id=self.chat.id, text=text,
            message_thread_id=self.message_thread_id, **kwargs,
        )

    async def reply(self, text: str, **kwargs):
        return await self.answer(text, **kwargs)

    async def answer_document(self, document, **kwargs):
        kwargs.pop("message_thread_id", None)
        return await self.bot.send_document(
            chat_id=self.chat.id, document=document,
            message_thread_id=self.message_thread_id, **kwargs,
        )


class ChatPump:
    """One inbox queue + one actor task per chat_id."""

    def __init__(
        self, pool: "SessionPool", renderer: "ResponseRenderer", bot: Any = None,
    ) -> None:
        self._pool = pool
        self._renderer = renderer
        self._bot = bot  # needed to build an OutboundAnchor for wake()
        self._inbox: dict[int, asyncio.Queue] = {}
        self._actors: dict[int, asyncio.Task] = {}

    def wake(self, chat_id: int, text: str) -> None:
        """Make the model act on an agent-facing EVENT (e.g. a reminder firing),
        resuming the session if it went idle. Runs as its OWN turn: if the user
        currently has a turn in flight it WAITS for that to finish (defer=True) so
        it never barges into their conversation. Status-agnostic — safe to call in
        any session state; the pump handles resume/idle/busy. Requires a bot."""
        if self._bot is None:
            logger.warning("ChatPump.wake called but no bot configured — dropping")
            return
        thread = self._pool.current_thread(chat_id)
        anchor = OutboundAnchor(self._bot, chat_id, thread)
        self.submit(PumpItem(
            chat_id=chat_id, message=anchor, text=text,
            apply_welcome=False, reply=False, defer=True,
        ))

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
        # Deferred items dequeued mid-turn wait here and run as their own turns,
        # in arrival order, once the current turn finishes.
        pending: list[PumpItem] = []
        get_task: asyncio.Task | None = None
        render: asyncio.Task | None = None
        typing: asyncio.Task | None = None
        try:
            while True:
                # Pick the next turn-STARTING item: drain deferred items first,
                # else take the next inbox message. `get_task` may be carried over
                # from the previous turn's drain (a pending inbox.get()) — reuse
                # it rather than dropping its message.
                if pending:
                    item = pending.pop(0)
                else:
                    if get_task is None:
                        get_task = asyncio.create_task(inbox.get())
                    item = await get_task
                    get_task = None

                try:
                    # ---- run a turn with `item` ----
                    session_id, text = await self._prepare_turn(item)
                    await self._pool.send_message(
                        session_id, text, attachments=item.attachments
                    )
                    render = asyncio.create_task(self._render(item, session_id))
                    # Keep a "typing…" indicator alive for the whole turn so a slow
                    # model turn shows life instead of looking frozen.
                    typing = asyncio.create_task(self._keep_typing(item))

                    # Drain the inbox WHILE the turn streams. A normal (user)
                    # message is injected into the LIVE turn (steering) and the
                    # running `render` shows the continuation. A DEFERRED item
                    # (e.g. a reminder) is set aside to run as its own turn after
                    # this one — it must not barge into the user's turn.
                    while True:
                        if get_task is None:
                            get_task = asyncio.create_task(inbox.get())
                        await asyncio.wait(
                            {render, get_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if render.done():
                            break  # turn ended; carry the pending get_task
                        mid = get_task.result()
                        get_task = None
                        if mid.defer:
                            pending.append(mid)
                        else:
                            await self._deliver_mid_turn(mid, session_id)

                    typing.cancel()
                    typing = None
                    ctx = render.result()
                    render = None
                    await self._post_turn(item, ctx)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — turn boundary
                    if typing is not None:
                        typing.cancel()
                        typing = None
                    render = None
                    await self._handle_error(item, e)
        except asyncio.CancelledError:
            pass
        finally:
            if get_task is not None and not get_task.done():
                get_task.cancel()
            if render is not None and not render.done():
                render.cancel()
            if typing is not None and not typing.done():
                typing.cancel()

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

    async def _keep_typing(self, item: PumpItem) -> None:
        """Re-send the 'typing…' chat action every few seconds while a turn runs,
        so a slow model turn shows life instead of looking frozen. PAUSES while the
        bot is awaiting the user (pending permission/clarification) — then it's the
        user's turn to act, not the bot working. Best-effort; never crashes a turn."""
        chat_id = item.chat_id
        bot = item.message.bot
        try:
            while True:
                if not self._renderer.is_awaiting_user(chat_id):
                    try:
                        await bot.send_chat_action(
                            chat_id=chat_id, action="typing",
                            message_thread_id=self._pool.current_thread(chat_id),
                        )
                    except Exception:  # noqa: BLE001 — typing is best-effort
                        pass
                await asyncio.sleep(_TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

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
