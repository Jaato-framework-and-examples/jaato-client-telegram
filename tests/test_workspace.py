"""
Tests for workspace management functionality.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jaato_client_telegram.config import Config
from jaato_client_telegram.workspace import Workspace, WorkspaceManager


@pytest.fixture
def temp_workspace_root():
    """Create a temporary directory for workspace testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # Create template files
        template_env = root / ".env"
        template_env.write_text("# Test env file\nTEST_VAR=value\n")
        
        template_jaato = root / ".jaato"
        template_jaato.mkdir()
        (template_jaato / "test.txt").write_text("test content")
        
        yield root


@pytest.fixture
def workspace_manager(temp_workspace_root):
    """Create a WorkspaceManager for testing."""
    mock_config = MagicMock(spec=Config)
    
    manager = WorkspaceManager(mock_config)
    # Override root for testing
    manager.root = temp_workspace_root / "workspaces"
    manager.template_env = temp_workspace_root / ".env"
    manager.template_jaato = temp_workspace_root / ".jaato"
    
    return manager


@pytest.mark.asyncio
async def test_workspace_creation(workspace_manager):
    """Test that a workspace is created correctly."""
    chat_id = 123456789
    workspace = workspace_manager.get_workspace(chat_id)
    
    # Workspace should not exist yet
    assert not workspace.exists
    
    # Create workspace
    await workspace.create()
    
    # Workspace should now exist
    assert workspace.exists
    assert workspace.path.is_dir()
    
    # Check .env was copied
    env_path = workspace.env_path
    assert env_path.exists()
    content = env_path.read_text()
    assert "TEST_VAR=value" in content
    
    # Check .jaato/ was copied
    jaato_path = workspace.path / ".jaato"
    assert jaato_path.exists()
    assert (jaato_path / "test.txt").exists()


@pytest.mark.asyncio
async def test_workspace_deletion(workspace_manager):
    """Test that a workspace can be deleted."""
    chat_id = 987654321
    workspace = workspace_manager.get_workspace(chat_id)
    
    # Create workspace
    await workspace.create()
    assert workspace.exists
    
    # Delete workspace
    await workspace.delete()
    assert not workspace.exists


@pytest.mark.asyncio
async def test_workspace_multiple_users(workspace_manager):
    """Test that multiple users get isolated workspaces."""
    chat_id_1 = 111111111
    chat_id_2 = 222222222
    
    workspace_1 = workspace_manager.get_workspace(chat_id_1)
    workspace_2 = workspace_manager.get_workspace(chat_id_2)
    
    # Create both workspaces
    await workspace_1.create()
    await workspace_2.create()
    
    # Both should exist
    assert workspace_1.exists
    assert workspace_2.exists
    
    # They should be in different directories
    assert workspace_1.path != workspace_2.path
    
    # Each should have their own .env
    assert workspace_1.env_path.exists()
    assert workspace_2.env_path.exists()
    
    # Modify user 1's .env
    workspace_1.env_path.write_text("USER1_VAR=value1")
    
    # User 2's .env should be unchanged
    content_2 = workspace_2.env_path.read_text()
    assert "USER1_VAR=value1" not in content_2


@pytest.mark.asyncio
async def test_workspace_manager_cleanup(workspace_manager):
    """Test that WorkspaceManager can clean up a workspace."""
    chat_id = 555555555
    workspace = workspace_manager.get_workspace(chat_id)
    
    # Create workspace
    await workspace.create()
    workspace_path = workspace.path  # Save path before deletion
    assert workspace_path.exists()
    
    # Clean up via manager
    await workspace_manager.cleanup_workspace(chat_id)
    
    # Workspace directory should be gone
    assert not workspace_path.exists()


def test_workspace_naming(workspace_manager):
    """Test that workspace directories are named correctly."""
    chat_id = 999999999
    workspace = workspace_manager.get_workspace(chat_id)
    
    # Create to set the path
    workspace._path = workspace.root / f"user_{chat_id}"
    
    # Check naming
    assert workspace.path.name == f"user_{chat_id}"
    assert str(workspace.path).endswith(f"/user_{chat_id}")
