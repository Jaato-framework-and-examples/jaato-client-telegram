"""
Whitelist management for jaato-client-telegram.

Manages user access control via username-based whitelist stored in JSON file.
"""

import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

from aiogram.types import Message

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


class WhitelistEntry(BaseModel):
    """A single whitelist entry."""

    username: str  # @username (without the @ prefix)
    added_by: str  # Admin who added this user
    added_at: str  # ISO timestamp


class AccessRequest(BaseModel):
    """A pending access request from a non-whitelisted user."""

    username: str | None  # @username (can be None if user has no username)
    first_name: str | None = None
    last_name: str | None = None
    user_id: int  # Telegram user ID
    chat_id: int  # Telegram chat ID
    requested_at: str  # ISO timestamp
    status: str = "pending"  # pending, approved, rejected
    message: str | None = None  # Optional introduction message from user


class WhitelistConfig(BaseModel):
    """Whitelist configuration and storage."""

    enabled: bool = True
    admin_usernames: list[str] = Field(default_factory=list)  # @alice, @bob
    entries: list[WhitelistEntry] = Field(default_factory=list)
    access_requests: list[AccessRequest] = Field(default_factory=list)  # Pending requests

    @classmethod
    def from_file(cls, path: Path) -> "WhitelistConfig":
        """Load whitelist from JSON file."""
        if not path.exists():
            logger.info(f"Whitelist file not found, creating default: {path}")
            default = cls()
            default.save(path)
            return default

        try:
            with path.open("r") as f:
                data = json.load(f)
            return cls(**data)
        except Exception as e:
            logger.error(f"Failed to load whitelist from {path}: {e}")
            # Return empty whitelist on error
            return cls()

    def save(self, path: Path) -> None:
        """Save whitelist to JSON file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as f:
                json.dump(self.model_dump(), f, indent=2)
            logger.info(f"Whitelist saved to {path}")
        except Exception as e:
            logger.error(f"Failed to save whitelist to {path}: {e}")
            raise

    def is_allowed(self, username: str | None) -> bool:
        """
        Check if a username is allowed to use the bot.

        Args:
            username: Telegram username (without @ prefix)

        Returns:
            True if whitelist is disabled, user is admin, or user is in whitelist
        """
        # If whitelist disabled, allow everyone
        if not self.enabled:
            return True

        # No username provided - deny access
        if not username:
            return False

        # Normalize username (strip @ prefix if present)
        username = username.lstrip("@")

        # Admins are always allowed
        if self.is_admin(username):
            return True

        # Check if username is in whitelist
        return any(entry.username == username for entry in self.entries)

    def is_admin(self, username: str | None) -> bool:
        """Check if a username is an admin."""
        if not username:
            return False
        # Normalize username (strip @ prefix)
        username = username.lstrip("@")
        return username in self.admin_usernames

    def add_user(self, username: str, added_by: str) -> None:
        """Add a user to the whitelist."""
        # Normalize username (remove @ prefix if present)
        username = username.lstrip("@")

        # Check if already exists
        if any(entry.username == username for entry in self.entries):
            raise ValueError(f"User @{username} is already whitelisted")

        # Add new entry
        from datetime import datetime

        entry = WhitelistEntry(
            username=username,
            added_by=added_by.lstrip("@"),
            added_at=datetime.utcnow().isoformat(),
        )
        self.entries.append(entry)
        logger.info(f"Added @{username} to whitelist by @{added_by}")

    def remove_user(self, username: str) -> None:
        """Remove a user from the whitelist."""
        # Normalize username
        username = username.lstrip("@")

        # Find and remove
        original_count = len(self.entries)
        self.entries = [e for e in self.entries if e.username != username]

        if len(self.entries) == original_count:
            raise ValueError(f"User @{username} is not in whitelist")

        logger.info(f"Removed @{username} from whitelist")

    def list_users(self) -> list[str]:
        """List all whitelisted usernames."""
        return [entry.username for entry in self.entries]

    def create_access_request(
        self,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        user_id: int,
        chat_id: int,
        message: str | None = None,
    ) -> AccessRequest:
        """Create a new access request."""
        from datetime import datetime

        request = AccessRequest(
            username=username,
            first_name=first_name,
            last_name=last_name,
            user_id=user_id,
            chat_id=chat_id,
            requested_at=datetime.utcnow().isoformat(),
            status="pending",
            message=message,
        )
        self.access_requests.append(request)
        logger.info(
            f"Access request created from {username or user_id} "
            f"(chat_id={chat_id})"
        )
        return request

    def get_pending_requests(self) -> list[AccessRequest]:
        """Get all pending access requests."""
        return [r for r in self.access_requests if r.status == "pending"]

    def find_request_by_user_id(self, user_id: int) -> AccessRequest | None:
        """Find an access request by user ID."""
        for request in self.access_requests:
            if request.user_id == user_id:
                return request
        return None

    def approve_request(self, user_id: int, approved_by: str) -> tuple[str, str]:
        """
        Approve an access request and add user to whitelist.

        Returns:
            Tuple of (username, added_by)
        """
        request = self.find_request_by_user_id(user_id)
        if not request:
            raise ValueError(f"No request found for user_id={user_id}")

        if request.status != "pending":
            raise ValueError(f"Request is not pending (status={request.status})")

        if not request.username:
            raise ValueError("Cannot approve request without username")

        # Mark request as approved
        request.status = "approved"

        # Add to whitelist
        self.add_user(request.username, approved_by)

        return (request.username, approved_by)

    def reject_request(self, user_id: int) -> AccessRequest:
        """Reject an access request."""
        request = self.find_request_by_user_id(user_id)
        if not request:
            raise ValueError(f"No request found for user_id={user_id}")

        request.status = "rejected"
        logger.info(f"Access request rejected for {request.username or request.user_id}")
        return request


class WhitelistManager:
    """
    Manages whitelist loading and access control.

    This class provides:
    - Loading whitelist from JSON file
    - Checking if users are allowed
    - Middleware for blocking non-whitelisted users
    """

    def __init__(self, path: str | None = None, bot=None):
        """
        Initialize whitelist manager.

        Args:
            path: Path to whitelist JSON file. If None, uses default location.
            bot: Optional aiogram Bot instance for sending admin notifications.
        """
        if path is None:
            self.path = Path("whitelist.json")
        else:
            self.path = Path(path)

        self.bot = bot  # Store bot instance for sending notifications
        self.config = WhitelistConfig.from_file(self.path)
        logger.info(
            f"Whitelist loaded: enabled={self.config.enabled}, "
            f"users={len(self.config.entries)}, admins={len(self.config.admin_usernames)}"
        )

    def reload(self) -> None:
        """Reload whitelist from file."""
        self.config = WhitelistConfig.from_file(self.path)
        logger.info("Whitelist reloaded from file")

    def save(self) -> None:
        """Save current whitelist to file."""
        self.config.save(self.path)

    def is_allowed(self, username: str | None) -> bool:
        """Check if a username is allowed to use the bot."""
        return self.config.is_allowed(username)

    def is_admin(self, username: str | None) -> bool:
        """Check if a username is an admin."""
        return self.config.is_admin(username)

    def add_user(self, username: str, added_by: str) -> None:
        """Add a user to the whitelist and save."""
        self.config.add_user(username, added_by)
        self.save()

    def remove_user(self, username: str) -> None:
        """Remove a user from the whitelist and save."""
        self.config.remove_user(username)
        self.save()

    def list_users(self) -> list[str]:
        """List all whitelisted usernames."""
        return self.config.list_users()

    def create_access_request(
        self,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        user_id: int,
        chat_id: int,
        message: str | None = None,
    ) -> AccessRequest:
        """Create a new access request."""
        request = self.config.create_access_request(
            username, first_name, last_name, user_id, chat_id, message
        )
        self.save()
        return request

    def get_pending_requests(self) -> list[AccessRequest]:
        """Get all pending access requests."""
        return self.config.get_pending_requests()

    def find_request_by_user_id(self, user_id: int) -> AccessRequest | None:
        """Find an access request by user ID."""
        return self.config.find_request_by_user_id(user_id)

    def approve_request(self, user_id: int, approved_by: str) -> tuple[str, str]:
        """
        Approve an access request and add user to whitelist.

        Returns:
            Tuple of (username, added_by)
        """
        username, added_by = self.config.approve_request(user_id, approved_by)
        self.save()
        return username, added_by

    def reject_request(self, user_id: int) -> AccessRequest:
        """Reject an access request."""
        request = self.config.reject_request(user_id)
        self.save()
        return request

    def create_middleware(
        self, silent: bool = True
    ) -> Callable:
        """
        Create aiogram middleware for whitelist checking.

        Args:
            silent: If True, silently ignore non-whitelisted users.
                   If False, send a polite request-for-access message.

        Returns:
            Middleware function for use with aiogram
        """

        async def middleware(
            handler: Callable,
            event: Message,
            data: dict,
        ) -> None:
            # Get username from message
            username = event.from_user.username if event.from_user else None

            # Check if allowed
            if not self.is_allowed(username):
                # User not in whitelist
                logger.info(f"Non-whitelisted user: @{username} (chat_id={event.chat.id})")

                # Check if there's already a pending request
                existing_request = self.find_request_by_user_id(event.from_user.id)

                if not silent:
                    if existing_request and existing_request.status == "pending":
                        # Already have a pending request
                        await event.answer(
                            "📝 Your access request is pending approval.\n\n"
                            "An administrator will review your request shortly. "
                            "You'll be notified once a decision is made."
                        )
                    elif not username:
                        # No username - can't process request
                        await event.answer(
                            "👋 Welcome! I noticed you don't have a Telegram username set.\n\n"
                            "To use this bot, you'll need to set a username in Telegram settings:\n"
                            "Settings → Edit Profile → Username\n\n"
                            "Please set one and try again!"
                        )
                    else:
                        # First contact - create access request and send polite message
                        self.create_access_request(
                            username=username,
                            first_name=event.from_user.first_name,
                            last_name=event.from_user.last_name,
                            user_id=event.from_user.id,
                            chat_id=event.chat.id,
                            message=None,  # Could be enhanced to capture first message
                        )

                        await event.answer(
                            f"👋 Welcome, {event.from_user.first_name}!\n\n"
                            f"Thank you for your interest in using this bot. "
                            f"Your username is @{username}.\n\n"
                            f"Your access request has been submitted to the administrators for approval. "
                            f"You'll be notified once your request is reviewed.\n\n"
                            f"📝 Request Status: Pending Approval"
                        )

                        # Notify admins
                        await self._notify_admins_of_request(event)

                # Don't call the handler - block the message
                return

            # User is allowed, proceed to handler
            await handler(event, data)

        return middleware

    async def _get_admin_chat_ids(self) -> dict[str, int]:
        """
        Resolve admin usernames to chat IDs.
        
        Returns a dict mapping username -> chat_id.
        Caches results to avoid repeated lookups.
        """
        # Check if we have a bot instance
        if not self.bot:
            logger.warning("No bot instance available for admin notifications")
            return {}
        
        # Cache for admin chat IDs (stored as instance variable)
        if not hasattr(self, '_admin_chat_cache'):
            self._admin_chat_cache = {}
        
        # Try to resolve any usernames we haven't cached yet
        for username in self.config.admin_usernames:
            normalized_username = username.lstrip("@")
            
            # Skip if already cached
            if normalized_username in self._admin_chat_cache:
                continue
            
            # Try to get chat ID from bot's get_chat method
            try:
                # Use get_chat to resolve username to chat ID
                chat = await self.bot.get_chat(f"@{normalized_username}")
                self._admin_chat_cache[normalized_username] = chat.id
                logger.debug(f"Resolved admin @{normalized_username} to chat_id={chat.id}")
            except Exception as e:
                logger.warning(f"Failed to resolve admin @{normalized_username}: {e}")
                # Use None to indicate failed resolution
                self._admin_chat_cache[normalized_username] = None
        
        # Return only successfully resolved chat IDs
        return {
            username: chat_id 
            for username, chat_id in self._admin_chat_cache.items() 
            if chat_id is not None
        }

    async def _notify_admins_of_request(self, event: Message) -> None:
        """
        Notify all admins about a new access request.
        
        Sends a Telegram message to each admin with approve/reject buttons.
        """
        # Skip if no bot instance
        if not self.bot:
            logger.warning("Cannot send admin notifications: no bot instance")
            return
        
        # Get admin chat IDs
        admin_chat_ids = await self._get_admin_chat_ids()
        
        if not admin_chat_ids:
            logger.warning("No admin chat IDs resolved - cannot send notifications")
            return
        
        # Build notification message
        username = event.from_user.username if event.from_user else "No username"
        first_name = event.from_user.first_name if event.from_user else "User"
        last_name = event.from_user.last_name if event.from_user else ""
        user_id = event.from_user.id
        chat_id = event.chat.id
        
        # Format the notification
        if username:
            user_identifier = f"@{username}"
        else:
            user_identifier = f"{first_name} {last_name}".strip()
        
        notification_text = (
            f"🔔 <b>New Access Request</b>\n\n"
            f"<b>User:</b> {user_identifier}\n"
            f"<b>User ID:</b> <code>{user_id}</code>\n"
            f"<b>Name:</b> {first_name} {last_name}".strip()
        )
        
        # Create inline keyboard with approve/reject buttons
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        
        keyboard = InlineKeyboardBuilder()
        keyboard.button(
            text="✅ Approve",
            callback_data=f"whitelist_approve_{user_id}"
        )
        keyboard.button(
            text="❌ Reject",
            callback_data=f"whitelist_reject_{user_id}"
        )
        
        # Send to each admin with inline keyboard
        successful_notifications = 0
        for admin_username, admin_chat_id in admin_chat_ids.items():
            try:
                await self.bot.send_message(
                    chat_id=admin_chat_id,
                    text=notification_text,
                    reply_markup=keyboard.as_markup(),
                    parse_mode="HTML"
                )
                successful_notifications += 1
                logger.info(f"Sent access request notification to admin @{admin_username}")
            except Exception as e:
                logger.error(f"Failed to notify admin @{admin_username} (chat_id={admin_chat_id}): {e}")
        
        if successful_notifications > 0:
            logger.info(
                f"✅ Notified {successful_notifications} admin(s) about access request from "
                f"@{username} ({first_name}, user_id={user_id})"
            )
        else:
            logger.warning("⚠️ Failed to notify any admins about access request")
