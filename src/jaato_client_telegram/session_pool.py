"""
Session pool for managing per-user jaato SDK clients.

Each Telegram user (chat_id) gets their own IPCRecoveryClient instance
to maintain isolated session state with the jaato server.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jaato_sdk.client import IPCRecoveryClient, ConnectionState
from jaato_client_telegram.config import JaatoConfig
from jaato_client_telegram.workspace import WorkspaceManager

if TYPE_CHECKING:
    from jaato_sdk.events import ClientConfigRequest


logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """Metadata about an active SDK client session."""

    client: IPCRecoveryClient
    created_at: datetime
    last_activity: datetime
    workspace_path: str  # Track workspace for cleanup


def create_telegram_presentation_context() -> dict:
    """
    Create presentation context for Telegram chat clients.

    Telegram mobile clients have narrow width (45 chars) and limited markdown.
    However, we support expandable blockquotes for wide content like JSON, code, tables.

    Returns:
        Dict suitable for ClientConfigRequest.presentation field
    """
    return {
        "content_width": 45,  # Mobile Telegram width
        "content_height": None,  # Scrollable
        "supports_markdown": True,  # Basic markdown (bold, italic, code, links)
        "supports_tables": False,  # Tables don't render well
        "supports_code_blocks": True,  # Inline code and code blocks
        "supports_images": True,  # Can display images
        "supports_rich_text": True,  # Bold, italic, underline, strikethrough
        "supports_unicode": True,  # Full Unicode support
        "supports_mermaid": False,  # No diagram support
        "supports_expandable_content": True,  # We handle wide content with expandable blockquotes
        "client_type": "chat",  # Messaging platform
    }


class SessionPool:
    """
    Manages a pool of jaato SDK client connections, one per Telegram user.

    The pool ensures each chat_id has an isolated IPCRecoveryClient instance
    for proper session state management in the jaato server.
    
    Each client runs in its own workspace directory with isolated .env and .jaato/.

    Connection Recovery Pattern:
    - Each IPCRecoveryClient calls set_session_id() after creating a session
    - When connections drop, IPCRecoveryClient automatically reconnects with exponential backoff
    - After reconnecting, the client reattaches to the same session_id
    - The server restores session state from disk, preserving conversation history
    - We do NOT recreate clients on disconnection - let IPCRecoveryClient handle it
    """

    def __init__(self, config: JaatoConfig, workspace_manager: WorkspaceManager, max_concurrent: int = 50):
        """
        Initialize the session pool.

        Args:
            config: jaato SDK connection configuration
            workspace_manager: Workspace manager for per-user directories
            max_concurrent: Maximum number of concurrent SDK clients
        """
        self._config = config
        self._workspace_manager = workspace_manager
        self._max_concurrent = max_concurrent
        self._sessions: dict[int, SessionInfo] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, chat_id: int) -> IPCRecoveryClient:
        """
        Get or create an SDK client for this chat_id.

        This method implements the correct connection recovery pattern:
        1. When creating a new client, it calls set_session_id() after create_session()
           so that IPCRecoveryClient can reattach to the same session after reconnection
        2. When the client is disconnected (state != CONNECTED), we do NOT recreate
           the client. Instead, we let IPCRecoveryClient handle automatic reconnection
           and session reattachment. The client will transition through:
           CONNECTED → RECONNECTING → CONNECTED (with same session_id)

        This preserves conversation state across temporary connection drops because:
        - IPCRecoveryClient uses exponential backoff to retry connections
        - After reconnecting, it sends session.attach with the stored session_id
        - The server restores the session from disk (if evicted from memory)
        - No new session is created, so conversation history is preserved

        Args:
            chat_id: Telegram chat ID (user identifier)

        Returns:
            IPCRecoveryClient instance for this user

        Raises:
            RuntimeError: If unable to create SDK client
        """
        async with self._lock:
            now = datetime.now()

            # Check if client already exists for this chat_id
            if chat_id in self._sessions:
                session = self._sessions[chat_id]
                
                # Update last activity timestamp
                session.last_activity = now
                
                # Return the client even if it's disconnected or reconnecting
                # IPCRecoveryClient will handle automatic reconnection
                # We do NOT recreate the client to preserve session state
                return session.client

            # Evict oldest session if at capacity
            if len(self._sessions) >= self._max_concurrent:
                await self._evict_oldest()

            # Create new SDK client
            try:
                # Get or create workspace for this user
                workspace = self._workspace_manager.get_workspace(chat_id)
                
                # Create workspace directory on first use
                if not workspace.exists:
                    await workspace.create()
                
                # Initialize SDK client with workspace_path parameter
                # This sets the working directory for the SDK client
                # The env_file is relative to the workspace directory
                client = IPCRecoveryClient(
                    socket_path=self._config.socket_path,
                    auto_start=self._config.auto_start,
                    env_file=".env",  # Always relative to workspace
                    workspace_path=workspace.path,  # Set workspace directory
                )
                await client.connect()

                # Create a dedicated session for this user
                session_id = await client.create_session(name=f"telegram-{chat_id}")
                if not session_id:
                    raise RuntimeError(
                        "Server failed to create session — check server logs "
                        "for provider configuration errors"
                    )

                # CRITICAL: Call set_session_id() so IPCRecoveryClient can
                # reattach to this session after connection drops
                # This is the key to preserving conversation state across reconnections
                client.set_session_id(session_id)

                self._sessions[chat_id] = SessionInfo(
                    client=client,
                    created_at=now,
                    last_activity=now,
                    workspace_path=str(workspace.path),
                )

                # Send presentation context to inform server about client capabilities
                # This allows the agent to adapt its output for Telegram's constraints
                try:
                    from jaato_sdk.events import ClientConfigRequest

                    presentation_ctx = create_telegram_presentation_context()
                    config_event = ClientConfigRequest(presentation=presentation_ctx)
                    await client.send_event(config_event)
                except ImportError:
                    # SDK version doesn't support presentation context yet
                    pass
                except Exception:
                    # Log but don't fail - presentation is optional
                    pass

                return client

            except Exception as e:
                raise RuntimeError(f"Failed to create SDK client for chat_id {chat_id}: {e}") from e

    async def remove_client(self, chat_id: int) -> None:
        """
        Disconnect and remove a client session.

        Also removes the user's workspace directory for complete cleanup.

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

            # Clean up workspace directory
            try:
                await self._workspace_manager.cleanup_workspace(chat_id)
            except Exception:
                # Log but don't fail - workspace may already be deleted
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
