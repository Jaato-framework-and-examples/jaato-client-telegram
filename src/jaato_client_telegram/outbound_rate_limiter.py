"""Proactive outbound rate limiting for the Telegram Bot API.

Telegram enforces roughly **1 message/second per chat** and ~30/second globally
for bots; exceeding it yields HTTP 429 (aiogram raises ``TelegramRetryAfter``).
Our renderer splits a long reply into multiple 4096-char messages and fires them
back-to-back, which trips the per-chat limit.

This aiogram *request* middleware sits on the ``Bot``'s session, so **every**
outbound API call funnels through it no matter which handler made it. It:

- **Proactively paces** message-creating sends (``sendMessage`` / ``sendPhoto`` /
  … and ``forward*`` / ``copy*``) per ``chat_id`` — a minimum gap so we never
  exceed 1/sec to a chat — plus a global fleet cap. Edits (streaming) and chat
  actions (typing) are **not** paced: they are lenient and must not consume a
  chat's message budget.
- **Reactively** honors any 429 that still slips through (bursts, the stricter
  group limits): sleep ``retry_after`` and retry, instead of surfacing an error.
"""

import asyncio
import logging
import time
from typing import Any

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter

logger = logging.getLogger(__name__)

# Message-creating methods count toward the per-chat 1/sec limit; edits and chat
# actions do not (and pacing them would cripple streaming edits / the typing
# indicator, which would then compete for a chat's send budget).
_UNPACED_SEND = frozenset({"sendChatAction"})
_PACED_PREFIXES = ("send", "forward", "copy")


def _is_message_send(method: Any) -> bool:
    name = getattr(method, "__api_method__", "")
    return name not in _UNPACED_SEND and name.startswith(_PACED_PREFIXES)


class OutboundRateLimiter(BaseRequestMiddleware):
    """Pace outbound message sends per chat (+ a global cap) and retry on 429."""

    def __init__(
        self,
        per_chat_interval: float = 1.05,
        global_min_interval: float = 1 / 25,
        max_retries: int = 5,
    ) -> None:
        self._per_chat_interval = per_chat_interval
        self._global_min_interval = global_min_interval
        self._max_retries = max_retries
        self._chat_next: dict[Any, float] = {}  # chat_id -> earliest next send (monotonic)
        self._global_next: float = 0.0
        self._lock = asyncio.Lock()

    async def _reserve(self, chat_id: Any) -> float:
        """Reserve the next send slot for ``chat_id`` (also honoring the global
        cap); return how long to sleep before actually sending."""
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._chat_next.get(chat_id, 0.0), self._global_next)
            self._chat_next[chat_id] = at + self._per_chat_interval
            self._global_next = at + self._global_min_interval
            return at - now

    async def __call__(self, make_request, bot, method):
        chat_id = getattr(method, "chat_id", None)
        if chat_id is not None and _is_message_send(method):
            wait = await self._reserve(chat_id)
            if wait > 0:
                await asyncio.sleep(wait)

        for attempt in range(self._max_retries):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                delay = e.retry_after + 0.5
                logger.warning(
                    "Telegram 429 on %s (chat=%s): sleeping %.1fs then retry (%d/%d)",
                    getattr(method, "__api_method__", "?"), chat_id, delay,
                    attempt + 1, self._max_retries,
                )
                # Push this chat's next slot past the cooldown so siblings queue behind it.
                if chat_id is not None:
                    async with self._lock:
                        self._chat_next[chat_id] = max(
                            self._chat_next.get(chat_id, 0.0), time.monotonic() + delay
                        )
                await asyncio.sleep(delay)
        # Final attempt — let a persistent failure propagate to the caller.
        return await make_request(bot, method)
