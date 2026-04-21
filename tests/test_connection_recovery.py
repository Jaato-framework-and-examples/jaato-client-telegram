"""
Test connection recovery in SessionPool.

Verifies that SessionPool properly implements the correct connection recovery pattern:
1. Calls set_session_id() after creating a session to enable reattachment
2. Returns the same client even when disconnected, letting IPCRecoveryClient handle reconnection
3. Does NOT recreate clients on disconnection (preserves session state)
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jaato_client_telegram.session_pool import SessionPool, SessionInfo
from jaato_client_telegram.config import JaatoConfig
from jaato_client_telegram.workspace import WorkspaceManager
from jaato_sdk.client import ConnectionState


@pytest.mark.asyncio
class TestConnectionRecovery:
    """Test that SessionPool implements correct connection recovery pattern."""

    async def test_get_client_calls_set_session_id(self):
        """Test that get_client calls set_session_id after creating a session."""
        config = JaatoConfig(socket_path="/tmp/test.sock", auto_start=False)
        workspace_manager = MagicMock(spec=WorkspaceManager)

        # Mock workspace
        mock_workspace = MagicMock()
        mock_workspace.exists = False
        workspace_manager.get_workspace.return_value = mock_workspace
        workspace_manager.cleanup_workspace = AsyncMock()
        mock_workspace.create = AsyncMock()

        pool = SessionPool(config, workspace_manager, max_concurrent=10)

        # Mock IPCRecoveryClient
        with patch('jaato_client_telegram.session_pool.IPCRecoveryClient') as MockClient:
            mock_client = MagicMock()
            mock_client.state = ConnectionState.CONNECTED
            mock_client.connect = AsyncMock()
            mock_client.create_session = AsyncMock(return_value="session-123")
            mock_client.send_event = AsyncMock()
            mock_client.set_session_id = MagicMock()
            MockClient.return_value = mock_client

            # Get client for new chat
            client = await pool.get_client(12345)

            # Verify client was created and set_session_id was called
            assert client is mock_client
            assert mock_client.connect.called
            assert mock_client.create_session.called
            assert mock_client.set_session_id.called
            mock_client.set_session_id.assert_called_once_with("session-123")

            # Verify session info was stored
            session_info = pool.get_session_info(12345)
            assert session_info is not None
            assert session_info.client is mock_client

    async def test_get_client_returns_same_client_when_disconnected(self):
        """
        Test that get_client returns the same client even when disconnected.

        This is the CORRECT pattern: we do NOT recreate clients on disconnection.
        Instead, we let IPCRecoveryClient handle automatic reconnection with session reattachment.
        """
        config = JaatoConfig(socket_path="/tmp/test.sock", auto_start=False)
        workspace_manager = MagicMock(spec=WorkspaceManager)

        # Mock workspace - first call creates it
        mock_workspace = MagicMock()
        mock_workspace.exists = False
        workspace_manager.get_workspace.return_value = mock_workspace
        mock_workspace.create = AsyncMock()
        workspace_manager.cleanup_workspace = AsyncMock()

        pool = SessionPool(config, workspace_manager, max_concurrent=10)

        # Mock IPCRecoveryClient
        with patch('jaato_client_telegram.session_pool.IPCRecoveryClient') as MockClient:
            mock_client = MagicMock()
            mock_client.state = ConnectionState.CONNECTED
            mock_client.connect = AsyncMock()
            mock_client.create_session = AsyncMock(return_value="session-123")
            mock_client.send_event = AsyncMock()
            mock_client.set_session_id = MagicMock()
            MockClient.return_value = mock_client

            # First call - creates client
            client1 = await pool.get_client(12345)
            assert client1 is mock_client
            assert mock_client.connect.call_count == 1
            assert mock_client.set_session_id.called

            # Simulate disconnection (client goes into RECONNECTING state)
            mock_client.state = ConnectionState.RECONNECTING

            # Second call - should return the SAME client, not recreate it
            client2 = await pool.get_client(12345)

            # Verify same client was returned (no new client created)
            assert client2 is mock_client
            assert client2 is client1
            assert mock_client.connect.call_count == 1, "Should not create new client on disconnection"
            assert mock_client.set_session_id.call_count == 1, "Should not call set_session_id again"

            # Verify workspace was preserved (not cleaned up)
            assert not workspace_manager.cleanup_workspace.called

    async def test_get_client_preserves_session_across_reconnection_states(self):
        """
        Test that session is preserved across all reconnection states.

        The client should be returned in CONNECTED, RECONNECTING, CONNECTING states.
        Only a new client is created for a NEW chat_id.
        """
        config = JaatoConfig(socket_path="/tmp/test.sock", auto_start=False)
        workspace_manager = MagicMock(spec=WorkspaceManager)

        mock_workspace = MagicMock()
        mock_workspace.exists = False
        workspace_manager.get_workspace.return_value = mock_workspace
        mock_workspace.create = AsyncMock()
        workspace_manager.cleanup_workspace = AsyncMock()

        pool = SessionPool(config, workspace_manager, max_concurrent=10)

        # Mock IPCRecoveryClient
        with patch('jaato_client_telegram.session_pool.IPCRecoveryClient') as MockClient:
            mock_client = MagicMock()
            mock_client.state = ConnectionState.CONNECTED
            mock_client.connect = AsyncMock()
            mock_client.create_session = AsyncMock(return_value="session-123")
            mock_client.send_event = AsyncMock()
            mock_client.set_session_id = MagicMock()
            MockClient.return_value = mock_client

            # Get client when CONNECTED
            client1 = await pool.get_client(12345)
            assert client1 is mock_client
            assert mock_client.connect.call_count == 1

            # Simulate RECONNECTING state
            mock_client.state = ConnectionState.RECONNECTING
            client2 = await pool.get_client(12345)
            assert client2 is mock_client
            assert client2 is client1
            assert mock_client.connect.call_count == 1

            # Simulate CONNECTING state
            mock_client.state = ConnectionState.CONNECTING
            client3 = await pool.get_client(12345)
            assert client3 is mock_client
            assert client3 is client1
            assert mock_client.connect.call_count == 1

            # Simulate DISCONNECTED state (before first reconnection attempt)
            mock_client.state = ConnectionState.DISCONNECTED
            client4 = await pool.get_client(12345)
            assert client4 is mock_client
            assert client4 is client1
            assert mock_client.connect.call_count == 1

            # Verify only one client was ever created for this chat_id
            assert mock_client.connect.call_count == 1
            assert mock_client.set_session_id.call_count == 1

    async def test_different_chat_ids_create_different_clients(self):
        """Test that different chat_ids create different clients."""
        config = JaatoConfig(socket_path="/tmp/test.sock", auto_start=False)
        workspace_manager = MagicMock(spec=WorkspaceManager)

        mock_workspace = MagicMock()
        mock_workspace.exists = False
        workspace_manager.get_workspace.return_value = mock_workspace
        mock_workspace.create = AsyncMock()

        pool = SessionPool(config, workspace_manager, max_concurrent=10)

        # Mock IPCRecoveryClient
        with patch('jaato_client_telegram.session_pool.IPCRecoveryClient') as MockClient:
            clients_created = []

            def create_client(*args, **kwargs):
                mock_client = MagicMock()
                mock_client.state = ConnectionState.CONNECTED
                mock_client.connect = AsyncMock()
                mock_client.create_session = AsyncMock(return_value=f"session-{len(clients_created)}")
                mock_client.send_event = AsyncMock()
                mock_client.set_session_id = MagicMock()
                clients_created.append(mock_client)
                return mock_client

            MockClient.side_effect = create_client

            # Create clients for different chat_ids
            client1 = await pool.get_client(12345)
            client2 = await pool.get_client(67890)

            # Verify different clients were created
            assert len(clients_created) == 2
            assert client1 is clients_created[0]
            assert client2 is clients_created[1]
            assert client1 is not client2

            # Verify each has its own session_id
            clients_created[0].set_session_id.assert_called_once_with("session-0")
            clients_created[1].set_session_id.assert_called_once_with("session-1")
