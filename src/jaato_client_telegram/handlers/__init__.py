"""
Message handlers for jaato-client-telegram.

This package contains aiogram Router instances for handling
different types of Telegram messages and commands.
"""

from jaato_client_telegram.handlers.admin import router as admin_router
from jaato_client_telegram.handlers.callbacks import router as callbacks_router
from jaato_client_telegram.handlers.commands import router as commands_router
from jaato_client_telegram.handlers.filters import MentionedMe
from jaato_client_telegram.handlers.group import router as group_router
from jaato_client_telegram.handlers.lifecycle import router as lifecycle_router
from jaato_client_telegram.handlers.private import router as private_router


__all__ = [
    "MentionedMe",
    "admin_router",
    "callbacks_router",
    "commands_router",
    "group_router",
    "lifecycle_router",
    "private_router",
]
