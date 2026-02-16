"""
Session pool for managing per-user jaato SDK clients.

Each Telegram user (chat_id) gets their own IPCRecoveryClient instance
to maintain isolated session state with the jaato server.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from jaato_sdk.client import IPCRecoveryClient
from jaato_client_telegram.config import JaatoConfig


@dataclass
class SessionInfo:
    """Metadata about an active SDK client session."""

    client: IPCRecoveryClient
    created_at: datetime
    last_activity: datetime


class SessionPool:
    """
    Manages a pool of jaato SDK client connections, one per Telegram user.

    The pool ensures each chat_id has an isolated IPCRecoveryClient instance
    for proper session state management in the jaato server.
    """

    def __init__(self, config: JaatoConfig, max_concurrent: int = 50):
        """
        Initialize the session pool.

        Args:
            config: jaato SDK connection configuration
            max_concurrent: Maximum number of concurrent SDK clients
        """
        self._config = config
        self._max_concurrent = max_concurrent
        self._sessions: dict[int, SessionInfo] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, chat_id: int) -> IPCRecoveryClient:
        """
        Get or create an SDK client for this chat_id.

        Args:
            chat_id: Telegram chat ID (user identifier)

        Returns:
            IPCRecoveryClient instance for this user

        Raises:
            RuntimeError: If unable to create SDK client
        """
        async with self._lock:
            now = datetime.now()

            # Return existing session if still active
            if chat_id in self._sessions:
                session = self._sessions[chat_id]
                session.last_activity = now
                return session.client

            # Evict oldest session if at capacity
            if len(self._sessions) >= self._max_concurrent:
                await self._evict_oldest()

            # Create new SDK client
            try:
                client = IPCRecoveryClient(
                    socket_path=self._config.socket_path,
                    auto_start=self._config.auto_start,
                    env_file=self._config.env_file,
                )
                await client.connect()

                # Create a dedicated session for this user
                session_id = await client.create_session(name=f"telegram-{chat_id}")
                if not session_id:
                    raise RuntimeError(
                        "Server failed to create session — check server logs "
                        "for provider configuration errors"
                    )

                self._sessions[chat_id] = SessionInfo(
                    client=client,
                    created_at=now,
                    last_activity=now,
                )

                return client

            except Exception as e:
                raise RuntimeError(f"Failed to create SDK client for chat_id {chat_id}: {e}") from e

    async def remove_client(self, chat_id: int) -> None:
        """
        Disconnect and remove a client session.

        Args:
            chat_id: Telegram chat ID
        """
        async with self._lock:
            session = self._sessions.pop(chat_id, None)
            if session is None:
                return

            try:
                await session.client.disconnect()
            except Exception:
                # Log but don't fail - client may already be disconnected
                pass

    async def _evict_oldest(self) -> None:
        """Evict the least recently used session to make room."""
        if not self._sessions:
            return

        # Find session with oldest last_activity
        oldest_chat_id = min(
            self._sessions.keys(),
            key=lambda cid: self._sessions[cid].last_activity,
        )

        await self.remove_client(oldest_chat_id)

    async def cleanup_idle(self, max_idle_minutes: int = 60) -> int:
        """
        Disconnect clients that have been idle too long.

        Args:
            max_idle_minutes: Maximum idle time before cleanup

        Returns:
            Number of sessions cleaned up
        """
        now = datetime.now()
        to_remove = []

        async with self._lock:
            for chat_id, session in self._sessions.items():
                idle_seconds = (now - session.last_activity).total_seconds()
                if idle_seconds > max_idle_minutes * 60:
                    to_remove.append(chat_id)

            for chat_id in to_remove:
                await self.remove_client(chat_id)

        return len(to_remove)

    async def shutdown(self) -> None:
        """Disconnect all clients and shutdown the pool."""
        async with self._lock:
            chat_ids = list(self._sessions.keys())

        for chat_id in chat_ids:
            await self.remove_client(chat_id)

    @property
    def active_count(self) -> int:
        """Return the number of active sessions."""
        return len(self._sessions)

    def get_session_info(self, chat_id: int) -> SessionInfo | None:
        """
        Get metadata about a session without updating activity.

        Args:
            chat_id: Telegram chat ID

        Returns:
            SessionInfo if session exists, None otherwise
        """
        return self._sessions.get(chat_id)
