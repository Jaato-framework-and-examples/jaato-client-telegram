"""
Session pool for managing per-user jaato sessions via WebSocket transport.

Each Telegram user (chat_id) gets an isolated session on the jaato server.
A single shared WebSocket connection multiplexes events for all sessions.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jaato_sdk.events import (
    SendMessageRequest,
    PermissionResponseRequest,
    ClarificationResponseRequest,
    ClientConfigRequest,
    StopRequest,
    SessionInfoEvent,
)

from jaato_client_telegram.transport import WSTransport

if TYPE_CHECKING:
    pass  # JaatoWSConfig available via config module


logger = logging.getLogger(__name__)



@dataclass
class SessionMetadata:
    """Metadata about an active session."""
    session_id: str
    created_at: datetime
    last_activity: datetime


def create_telegram_presentation_context() -> dict:
    """Create presentation context for Telegram chat clients."""
    return {
        "content_width": 45,
        "content_height": None,
        "supports_markdown": True,
        "supports_tables": False,
        "supports_code_blocks": True,
        "supports_images": True,
        "supports_rich_text": True,
        "supports_unicode": True,
        "supports_mermaid": False,
        "supports_expandable_content": True,
        "client_type": "chat",
    }


class SessionPool:
    """Manages per-user sessions via a single WebSocket connection.

    Each Telegram user (chat_id) maps to a session_id on the server.
    Events are dispatched to per-session queues via WSTransport.
    """

    def __init__(self, transport: WSTransport, max_concurrent: int = 50) -> None:
        self._transport = transport
        self._max_concurrent = max_concurrent
        self._sessions: dict[int, SessionMetadata] = {}
        self._lock = asyncio.Lock()
        self._pending_session_future: asyncio.Future | None = None

    async def get_or_create_session(self, chat_id: int) -> str:
        """Get existing session_id or create a new session on the server."""
        async with self._lock:
            if chat_id in self._sessions:
                self._sessions[chat_id].last_activity = datetime.now()
                return self._sessions[chat_id].session_id

            if not self._transport.connected:
                await self._transport.connect()

            if len(self._sessions) >= self._max_concurrent:
                await self._evict_oldest()

            try:
                presentation_ctx = create_telegram_presentation_context()
                config_event = ClientConfigRequest(presentation=presentation_ctx)
                await self._transport.send(config_event)
                logger.info("Sent presentation context for chat_id %d", chat_id)

                send_req = SendMessageRequest(text="/start")
                await self._transport.send(send_req)

                session_id = await self._wait_for_session_id(timeout=10.0)

                self._transport.register_session(session_id)
                self._sessions[chat_id] = SessionMetadata(
                    session_id=session_id,
                    created_at=datetime.now(),
                    last_activity=datetime.now(),
                )
                logger.info("Created session %s for chat_id %d", session_id, chat_id)
                return session_id
            except Exception as e:
                raise RuntimeError(
                    f"Failed to create session for chat_id {chat_id}: {e}"
                ) from e

    async def _wait_for_session_id(self, timeout: float = 10.0) -> str:
        """Wait for a SessionInfoEvent from the server."""
        future = asyncio.get_event_loop().create_future()
        self._pending_session_future = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_session_future = None

    def on_session_info_event(self, event: SessionInfoEvent) -> None:
        """Called by transport when a SessionInfoEvent arrives."""
        if self._pending_session_future and not self._pending_session_future.done():
            self._pending_session_future.set_result(event.session_id)

    async def send_message(self, session_id: str, text: str) -> None:
        """Send a user message to the jaato server."""
        request = SendMessageRequest(text=text)
        await self._transport.send(request)

    async def respond_to_permission(
        self, session_id: str, request_id: str,
        response: str, edited_arguments: dict | None = None,
    ) -> None:
        """Send a permission response to the jaato server."""
        request = PermissionResponseRequest(
            request_id=request_id, response=response,
        )
        await self._transport.send(request)

    async def respond_to_clarification(
        self, session_id: str, request_id: str, responses: dict,
    ) -> None:
        """Send a clarification response to the jaato server."""
        request = ClarificationResponseRequest(
            request_id=request_id, responses=responses,
        )
        await self._transport.send(request)

    async def events(self, session_id: str) -> AsyncIterator:
        """Yield events for a specific session."""
        return self._transport.events(session_id)

    async def stop(self, session_id: str) -> None:
        """Stop the current agent execution."""
        request = StopRequest()
        await self._transport.send(request)

    def get_session_id(self, chat_id: int) -> str | None:
        """Get the session_id for a chat_id without updating activity."""
        session = self._sessions.get(chat_id)
        return session.session_id if session else None

    async def remove_client(self, chat_id: int) -> None:
        """Remove a session and clean up its queue."""
        async with self._lock:
            session = self._sessions.pop(chat_id, None)
            if session:
                self._transport.unregister_session(session.session_id)

    async def _evict_oldest(self) -> None:
        """Evict the least recently used session."""
        if not self._sessions:
            return
        oldest = min(
            self._sessions, key=lambda c: self._sessions[c].last_activity,
        )
        await self.remove_client(oldest)

    async def cleanup_idle(self, max_idle_minutes: int = 60) -> int:
        """Disconnect sessions that have been idle too long."""
        now = datetime.now()
        to_remove = []
        async with self._lock:
            for chat_id, session in self._sessions.items():
                if (now - session.last_activity).total_seconds() > max_idle_minutes * 60:
                    to_remove.append(chat_id)
        for chat_id in to_remove:
            await self.remove_client(chat_id)
        return len(to_remove)

    async def shutdown(self) -> None:
        """Disconnect all sessions and close the transport."""
        async with self._lock:
            chat_ids = list(self._sessions.keys())
        for chat_id in chat_ids:
            await self.remove_client(chat_id)
        await self._transport.disconnect()

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def get_session_info(self, chat_id: int) -> SessionMetadata | None:
        return self._sessions.get(chat_id)
