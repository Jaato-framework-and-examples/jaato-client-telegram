"""Session pool for managing per-user jaato sessions via WebSocket transport.

Each Telegram user (chat_id) gets its own isolated WebSocket connection
and session on the jaato server.  This matches the server's 1-client-1-session
model and avoids the multi-tenancy routing issues of a single shared WS.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from jaato_sdk.events import (
    SendMessageRequest,
    PermissionResponseRequest,
    ClarificationResponseRequest,
    ClientConfigRequest,
    StopRequest,
    SessionInfoEvent,
    StageFilesEvent,
    StagedFileSpec,
)

from jaato_client_telegram.host_tools import TOOL_SCHEMAS, TOOL_CATEGORIES, create_tool_executors
from jaato_client_telegram.transport import WSTransport

if TYPE_CHECKING:
    from aiogram import Bot
    from jaato_client_telegram.config import FileSharingConfig, JaatoWSConfig


logger = logging.getLogger(__name__)


@dataclass
class SessionMetadata:
    session_id: str
    created_at: datetime
    last_activity: datetime
    transport: WSTransport


def create_telegram_presentation_context() -> dict:
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
    """Manages per-user sessions, each with its own WebSocket connection."""

    def __init__(
        self,
        ws_config: "JaatoWSConfig",
        bot: "Bot | None" = None,
        file_config: "FileSharingConfig | None" = None,
        max_concurrent: int = 50,
    ) -> None:
        self._ws_config = ws_config
        self._bot = bot
        self._file_config = file_config
        self._max_concurrent = max_concurrent
        self._sessions: dict[int, SessionMetadata] = {}
        self._lock = asyncio.Lock()
        self._pending_session_future: asyncio.Future | None = None

    def set_bot(self, bot: "Bot", file_config: "FileSharingConfig | None" = None) -> None:
        self._bot = bot
        self._file_config = file_config

    def _make_transport(self) -> WSTransport:
        return WSTransport(
            url=self._ws_config.url,
            tls_config=self._ws_config.tls,
            keycloak_base_url=self._ws_config.keycloak_base_url,
            keycloak_realm=self._ws_config.keycloak_realm,
            keycloak_client_id=self._ws_config.keycloak_client_id,
            keycloak_client_secret=self._ws_config.keycloak_client_secret,
            secret_token=self._ws_config.secret_token,
        )

    async def get_or_create_session(self, chat_id: int) -> str:
        async with self._lock:
            if chat_id in self._sessions:
                self._sessions[chat_id].last_activity = datetime.now()
                return self._sessions[chat_id].session_id

            if len(self._sessions) >= self._max_concurrent:
                await self._evict_oldest()

            try:
                transport = self._make_transport()
                await transport.connect()

                presentation_ctx = create_telegram_presentation_context()
                config_event = ClientConfigRequest(presentation=presentation_ctx)
                await transport.send(config_event)
                logger.info("Sent presentation context for chat_id %d", chat_id)

                session_id = await transport.create_session()
                transport.register_session(session_id)

                if self._bot and self._file_config:
                    executors = create_tool_executors(self._bot, chat_id, self._file_config)
                    transport.set_session_tool_executors(session_id, executors)
                    await transport.register_host_tools(TOOL_SCHEMAS, TOOL_CATEGORIES)
                    logger.info("Registered host tools for session %s", session_id)

                self._sessions[chat_id] = SessionMetadata(
                    session_id=session_id,
                    created_at=datetime.now(),
                    last_activity=datetime.now(),
                    transport=transport,
                )

                logger.info("Created session %s for chat_id %d", session_id, chat_id)
                return session_id
            except Exception as e:
                raise RuntimeError(
                    f"Failed to create session for chat_id {chat_id}: {e}"
                ) from e

    def on_session_info_event(self, event: SessionInfoEvent) -> None:
        if self._pending_session_future and not self._pending_session_future.done():
            self._pending_session_future.set_result(event.session_id)

    async def send_message(self, session_id: str, text: str) -> None:
        transport = self._find_transport(session_id)
        request = SendMessageRequest(text=text)
        await transport.send(request)

    async def respond_to_permission(
        self, session_id: str, request_id: str,
        response: str, edited_arguments: dict | None = None,
    ) -> None:
        transport = self._find_transport(session_id)
        request = PermissionResponseRequest(
            request_id=request_id, response=response,
        )
        await transport.send(request)

    async def respond_to_clarification(
        self, session_id: str, request_id: str, responses: dict,
    ) -> None:
        transport = self._find_transport(session_id)
        request = ClarificationResponseRequest(
            request_id=request_id, responses=responses,
        )
        await transport.send(request)

    async def events(self, session_id: str) -> AsyncIterator:
        return self._find_transport(session_id).events(session_id)

    async def stop(self, session_id: str) -> None:
        transport = self._find_transport(session_id)
        request = StopRequest()
        await transport.send(request)

    async def stage_files(
        self,
        chat_id: int,
        files: list[tuple[str, bytes, str | None]],
    ) -> StageFilesEvent:
        if chat_id not in self._sessions:
            raise RuntimeError(f"No session for chat_id {chat_id}")
        transport = self._sessions[chat_id].transport
        specs = [
            StagedFileSpec(name=name, size=len(data), content_type=ct)
            for name, data, ct in files
        ]
        payloads = [data for _, data, _ in files]
        return await transport.stage_files(
            workspace_id="",
            specs=specs,
            payloads=payloads,
        )

    def get_session_id(self, chat_id: int) -> str | None:
        session = self._sessions.get(chat_id)
        return session.session_id if session else None

    def _find_transport(self, session_id: str) -> WSTransport:
        for meta in self._sessions.values():
            if meta.session_id == session_id:
                return meta.transport
        raise RuntimeError(f"No transport for session_id {session_id}")

    async def remove_client(self, chat_id: int) -> None:
        async with self._lock:
            session = self._sessions.pop(chat_id, None)
            if session:
                try:
                    await session.transport.disconnect()
                except Exception:
                    logger.exception("Error disconnecting transport for chat_id %d", chat_id)

    async def _evict_oldest(self) -> None:
        if not self._sessions:
            return
        oldest = min(
            self._sessions, key=lambda c: self._sessions[c].last_activity,
        )
        await self.remove_client(oldest)

    async def cleanup_idle(self, max_idle_minutes: int = 60) -> int:
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
        async with self._lock:
            chat_ids = list(self._sessions.keys())
        for chat_id in chat_ids:
            await self.remove_client(chat_id)

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def get_session_info(self, chat_id: int) -> SessionMetadata | None:
        return self._sessions.get(chat_id)
