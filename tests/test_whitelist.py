"""
Tests for whitelist management functionality.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from jaato_client_telegram.whitelist import (
    WhitelistConfig,
    WhitelistEntry,
    WhitelistManager,
)


class TestWhitelistConfig:
    """Test WhitelistConfig model."""

    def test_default_config(self):
        """Test creating default empty whitelist."""
        config = WhitelistConfig()

        assert config.enabled is True
        assert config.admin_usernames == []
        assert config.entries == []

    def test_from_dict(self):
        """Test creating config from dictionary."""
        data = {
            "enabled": True,
            "admin_usernames": ["alice", "bob"],
            "entries": [
                {
                    "username": "alice",
                    "added_by": "system",
                    "added_at": "2024-01-01T00:00:00",
                }
            ],
        }

        config = WhitelistConfig(**data)

        assert config.enabled is True
        assert config.admin_usernames == ["alice", "bob"]
        assert len(config.entries) == 1
        assert config.entries[0].username == "alice"

    def test_is_allowed_disabled(self):
        """Test that disabled whitelist allows everyone."""
        config = WhitelistConfig(enabled=False)

        assert config.is_allowed("anyone") is True
        assert config.is_allowed(None) is True
        assert config.is_allowed("") is True

    def test_is_allowed_enabled_empty(self):
        """Test that enabled but empty whitelist allows no one."""
        config = WhitelistConfig(enabled=True)

        assert config.is_allowed("alice") is False
        assert config.is_allowed(None) is False
        assert config.is_allowed("") is False

    def test_is_allowed_with_entries(self):
        """Test whitelist with entries."""
        config = WhitelistConfig(
            enabled=True,
            entries=[
                WhitelistEntry(username="alice", added_by="system", added_at="2024-01-01"),
            ],
        )

        assert config.is_allowed("alice") is True
        assert config.is_allowed("bob") is False

    def test_is_admin(self):
        """Test admin checking."""
        config = WhitelistConfig(admin_usernames=["alice", "bob"])

        assert config.is_admin("alice") is True
        assert config.is_admin("bob") is True
        assert config.is_admin("charlie") is False
        assert config.is_admin(None) is False
        assert config.is_admin("") is False

    def test_add_user(self):
        """Test adding user to whitelist."""
        config = WhitelistConfig()

        config.add_user("alice", "bob")

        assert len(config.entries) == 1
        assert config.entries[0].username == "alice"
        assert config.entries[0].added_by == "bob"
        assert config.entries[0].added_at is not None

    def test_add_user_duplicate(self):
        """Test adding duplicate user raises error."""
        config = WhitelistConfig()
        config.add_user("alice", "bob")

        with pytest.raises(ValueError, match="already whitelisted"):
            config.add_user("alice", "bob")

    def test_add_user_normalizes_username(self):
        """Test that @ prefix is stripped when adding."""
        config = WhitelistConfig()

        config.add_user("@alice", "bob")

        assert config.entries[0].username == "alice"
        assert config.is_allowed("alice") is True
        assert config.is_allowed("@alice") is True

    def test_remove_user(self):
        """Test removing user from whitelist."""
        config = WhitelistConfig()
        config.add_user("alice", "system")
        config.add_user("bob", "system")

        config.remove_user("alice")

        assert len(config.entries) == 1
        assert config.entries[0].username == "bob"
        assert config.is_allowed("alice") is False

    def test_remove_user_not_found(self):
        """Test removing non-existent user raises error."""
        config = WhitelistConfig()

        with pytest.raises(ValueError, match="not in whitelist"):
            config.remove_user("alice")

    def test_list_users(self):
        """Test listing whitelisted users."""
        config = WhitelistConfig()
        config.add_user("alice", "system")
        config.add_user("bob", "system")

        users = config.list_users()

        assert set(users) == {"alice", "bob"}


class TestWhitelistManager:
    """Test WhitelistManager."""

    @pytest.fixture
    def temp_whitelist_file(self, tmp_path: Path):
        """Create a temporary whitelist file."""
        data = {
            "enabled": True,
            "admin_usernames": ["admin"],
            "entries": [
                {
                    "username": "alice",
                    "added_by": "admin",
                    "added_at": "2024-01-01T00:00:00",
                }
            ],
        }

        file_path = tmp_path / "whitelist.json"
        file_path.write_text(json.dumps(data))
        return file_path

    def test_load_from_file(self, temp_whitelist_file):
        """Test loading whitelist from file."""
        manager = WhitelistManager(str(temp_whitelist_file))

        assert manager.config.enabled is True
        assert len(manager.config.entries) == 1
        assert manager.is_allowed("alice") is True
        assert manager.is_admin("admin") is True

    def test_create_default_if_not_exists(self, tmp_path):
        """Test that default whitelist is created if file doesn't exist."""
        file_path = tmp_path / "new_whitelist.json"

        manager = WhitelistManager(str(file_path))

        assert file_path.exists()
        assert manager.config.enabled is True
        assert len(manager.config.entries) == 0

    def test_is_allowed(self, temp_whitelist_file):
        """Test is_allowed proxy method."""
        manager = WhitelistManager(str(temp_whitelist_file))

        assert manager.is_allowed("alice") is True
        assert manager.is_allowed("bob") is False

    def test_is_admin(self, temp_whitelist_file):
        """Test is_admin proxy method."""
        manager = WhitelistManager(str(temp_whitelist_file))

        assert manager.is_admin("admin") is True
        assert manager.is_admin("alice") is False

    def test_add_user_saves(self, temp_whitelist_file):
        """Test that add_user saves to file."""
        manager = WhitelistManager(str(temp_whitelist_file))

        manager.add_user("bob", "admin")

        # Reload from file
        manager2 = WhitelistManager(str(temp_whitelist_file))
        assert manager2.is_allowed("bob") is True

    def test_remove_user_saves(self, temp_whitelist_file):
        """Test that remove_user saves to file."""
        manager = WhitelistManager(str(temp_whitelist_file))

        manager.remove_user("alice")

        # Reload from file
        manager2 = WhitelistManager(str(temp_whitelist_file))
        assert manager2.is_allowed("alice") is False

    def test_reload(self, temp_whitelist_file):
        """Test reloading whitelist from file."""
        manager = WhitelistManager(str(temp_whitelist_file))

        # Modify file externally
        data = json.loads(temp_whitelist_file.read_text())
        data["entries"].append({
            "username": "bob",
            "added_by": "admin",
            "added_at": "2024-01-02T00:00:00",
        })
        temp_whitelist_file.write_text(json.dumps(data))

        # Reload
        manager.reload()

        assert manager.is_allowed("bob") is True

    def test_list_users(self, temp_whitelist_file):
        """Test list_users proxy method."""
        manager = WhitelistManager(str(temp_whitelist_file))

        users = manager.list_users()

        assert users == ["alice"]


class TestWhitelistMiddleware:
    """Test whitelist middleware."""

    @pytest.fixture
    def whitelist_manager(self, tmp_path):
        """Create a whitelist manager for testing."""
        file_path = tmp_path / "test_whitelist.json"
        manager = WhitelistManager(str(file_path))
        manager.config.admin_usernames = ["admin"]
        manager.config.entries = [
            WhitelistEntry(username="alice", added_by="admin", added_at="2024-01-01"),
        ]
        manager.save()
        return manager

    @pytest.mark.asyncio
    async def test_middleware_allows_whitelisted(self, whitelist_manager):
        """Test that middleware allows whitelisted users."""
        middleware = whitelist_manager.create_middleware(silent=False)

        # Mock message from whitelisted user
        message = MagicMock()
        message.from_user.username = "alice"
        message.chat.id = 123
        message.answer = AsyncMock()

        # Mock handler
        handler = AsyncMock()

        # Call middleware
        await middleware(message, handler)

        # Handler should be called
        handler.assert_called_once_with(message)
        # No rejection message should be sent
        message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_blocks_non_whitelisted_silent(self, whitelist_manager):
        """Test that middleware blocks non-whitelisted users silently."""
        middleware = whitelist_manager.create_middleware(silent=True)

        # Mock message from non-whitelisted user
        message = MagicMock()
        message.from_user.username = "bob"
        message.chat.id = 123
        message.answer = AsyncMock()

        # Mock handler
        handler = AsyncMock()

        # Call middleware
        await middleware(message, handler)

        # Handler should NOT be called
        handler.assert_not_called()
        # No message should be sent (silent mode)
        message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_middleware_blocks_non_whitelisted_verbose(self, whitelist_manager):
        """Test that middleware blocks non-whitelisted users with message."""
        middleware = whitelist_manager.create_middleware(silent=False)

        # Mock message from non-whitelisted user
        message = MagicMock()
        message.from_user.username = "bob"
        message.chat.id = 123
        message.answer = AsyncMock()

        # Mock handler
        handler = AsyncMock()

        # Call middleware
        await middleware(message, handler)

        # Handler should NOT be called
        handler.assert_not_called()
        # Rejection message should be sent
        message.answer.assert_called_once()
        args = message.answer.call_args[0]
        assert "not authorized" in args[0].lower()

    @pytest.mark.asyncio
    async def test_middleware_blocks_no_username(self, whitelist_manager):
        """Test that middleware blocks users without username."""
        middleware = whitelist_manager.create_middleware(silent=False)

        # Mock message from user without username
        message = MagicMock()
        message.from_user.username = None
        message.chat.id = 123
        message.answer = AsyncMock()

        # Mock handler
        handler = AsyncMock()

        # Call middleware
        await middleware(message, handler)

        # Handler should NOT be called
        handler.assert_not_called()
        # Rejection message should be sent
        message.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_middleware_disabled_whitelist(self, whitelist_manager):
        """Test that middleware allows all when whitelist disabled."""
        # Disable whitelist
        whitelist_manager.config.enabled = False

        middleware = whitelist_manager.create_middleware(silent=False)

        # Mock message from non-whitelisted user
        message = MagicMock()
        message.from_user.username = "bob"
        message.chat.id = 123
        message.answer = AsyncMock()

        # Mock handler
        handler = AsyncMock()

        # Call middleware
        await middleware(message, handler)

        # Handler should be called (whitelist disabled)
        handler.assert_called_once_with(message)
