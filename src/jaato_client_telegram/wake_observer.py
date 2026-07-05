"""WakeObserver — the client half of the review-wake render/act layer.

Holds ONE persistent cascade-observer connection registered for the bot's cascade
id (``SessionPool.bot_cid``), listening for ``SessionWokenEvent``. When the daemon
wakes one of the bot's cold sessions (a reviewer commented on its store PR), the
observer routes ``session_id -> chat`` and asks the pump to RE-ATTACH + render the
daemon-driven *deferred turn* — so the model can run host tools (``share_tool``)
with the bot present to serve them.

Why a separate always-on connection: the bot idle-detaches per-chat sessions to
free server runners, so on a cold session there is no attached client to hear the
wake. This one connection is NOT attached to any session — it observes the cascade,
which the daemon routes lifecycle events to (the same tier ``SessionTerminatedEvent``
uses). Durable by construction: the daemon exempts a cid with a live wake binding
from its cascade-observer sweep, keeps the wake pending (bounded by the binding TTL,
days), and RE-EMITS it whenever the observer (re)registers — so a briefly-dropped
bot never loses a wake. We re-register on a cadence to cover reconnects (the
registration is per-connection) and to trigger that re-emit.

See docs/design/pr-review-feedback-loop.md ("The render/act layer").
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from jaato_sdk.events import EventType

if TYPE_CHECKING:
    from jaato_client_telegram.chat_pump import ChatPump
    from jaato_client_telegram.session_pool import SessionPool

logger = logging.getLogger(__name__)

# Re-register on this cadence: the cascade-observer registration is per-connection
# (cleaned on drop), and re-registering also re-nudges the daemon to re-emit any
# pending wake — so this doubles as reconnect recovery + pending-wake pickup.
_REREGISTER_INTERVAL = 45.0


class WakeObserver:
    def __init__(
        self,
        make_client: Callable[[], Any],
        cid: str,
        pool: "SessionPool",
        pump: "ChatPump",
    ) -> None:
        self._make_client = make_client
        self._cid = cid
        self._pool = pool
        self._pump = pump
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        """Launch the observer loop (call once, after the pool + pump exist)."""
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self._connect_and_observe()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — keep the observer alive across faults
                logger.exception("WakeObserver: loop error; retrying")
            if not self._stopped:
                await asyncio.sleep(5)

    async def _connect_and_observe(self) -> None:
        client = self._make_client()
        if not await client.connect():
            logger.warning("WakeObserver: connect failed; will retry")
            return
        self._client = client
        # subscribe survives the recovery client's internal reconnects.
        client.subscribe(EventType.SESSION_WOKEN, self._on_woken)
        logger.info("WakeObserver: observing cascade %s for SessionWokenEvent", self._cid)
        while not self._stopped and (client.is_connected or client.is_reconnecting):
            try:
                # NOTE: cascade observers filter by the event CLASS NAME
                # ("SessionWokenEvent"), NOT the EventType value ("session.woken").
                # Registering the value silently drops the event at the cascade
                # filter before it reaches our subscribe() handler.
                await client.execute_command(
                    "cascade.register", [self._cid, "observer", "SessionWokenEvent"]
                )
            except Exception:  # noqa: BLE001 — transient; retry next cycle
                logger.debug("WakeObserver: cascade.register failed", exc_info=True)
            await asyncio.sleep(_REREGISTER_INTERVAL)

    def _on_woken(self, event: Any) -> None:
        """A cold session was woken (deferred-turn pending). Re-attach its chat so
        the pump renders the driven turn with the bot present to serve host tools."""
        session_id = getattr(event, "session_id", "") or ""
        if not session_id:
            return
        chat_id = self._pool.chat_for_session(session_id)
        if chat_id is None:
            logger.warning(
                "WakeObserver: SessionWokenEvent for session %s maps to no known chat",
                session_id,
            )
            return
        logger.info(
            "WakeObserver: waking chat %s (session %s, %s) — re-attach + render",
            chat_id, session_id, getattr(event, "wake_ref", ""),
        )
        self._pump.wake_render(chat_id)

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
