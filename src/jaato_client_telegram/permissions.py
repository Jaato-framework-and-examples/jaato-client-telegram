"""
Permission approval UI for jaato-client-telegram.

Handles permission requests from the jaato agent:
- Detects permission request events
- Displays inline keyboard with Approve/Deny buttons
- Routes user decisions back to jaato via SDK
"""

import html
import logging
import re
from dataclasses import dataclass
from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from jaato_sdk.events import PermissionRequestedEvent


logger = logging.getLogger(__name__)


# ANSI escape code pattern - matches terminal color codes
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*m|\[\d+(?:;\d+)*m')


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE_PATTERN.sub('', text)


def format_tool_params(params: dict, max_width: int = 40) -> list[str]:
    """
    Format tool parameters for display, one per line, ellipsized.

    Args:
        params: Dictionary of parameter names to values
        max_width: Maximum line width before ellipsis (default 40 chars)

    Returns:
        List of formatted parameter lines
    """
    if not params:
        return []

    lines = []
    for key, value in params.items():
        value_str = str(value)
        # Calculate available space: "  • key: " prefix
        prefix_len = 4 + len(key) + 2  # "  • " + key + ": "
        available = max_width - prefix_len

        if available < 10:
            # Very long key - just show key with ellipsis
            lines.append(f"  • <code>{key}</code>: …")
        elif len(value_str) > available:
            # Ellipsize the value first, then strip ANSI and escape HTML
            ellipsis_len = 1  # "…" character
            truncated = value_str[:available - ellipsis_len - 2] + "…"
            clean_truncated = strip_ansi_codes(truncated)
            escaped_truncated = html.escape(clean_truncated, quote=False)
            lines.append(f"  • <code>{key}</code>: {escaped_truncated}")
        else:
            # Strip ANSI codes and escape HTML in value to prevent parsing errors
            clean_value = strip_ansi_codes(value_str)
            escaped_value = html.escape(clean_value, quote=False)
            lines.append(f"  • <code>{key}</code>: {escaped_value}")

    return lines


@dataclass
class PendingPermission:
    """A pending permission request waiting for user response."""

    request_id: str
    tool_name: str
    tool_args: dict[str, Any]
    prompt_lines: list[str] | None
    warnings: str | None
    warning_level: str | None
    response_options: list[dict[str, str]]
    chat_id: int
    message_id: int  # Telegram message to edit when resolved


class PermissionHandler:
    """
    Manages permission requests and UI state.

    This handler:
    - Tracks pending permission requests per chat
    - Formats permission requests for display
    - Creates inline keyboards for user response
    - Routes user decisions back to jaato
    """

    # Telegram inline keyboard unsupported actions
    # These action types don't work well with simple button clicks
    # and require more complex UI (text input, modal dialogs, etc.)
    _DEFAULT_UNSUPPORTED_ACTIONS = {
        "comment",      # Requires free-form text input
        "allow_comment", # Requires free-form text input
        "edit",         # Requires editing existing text
        "modify",       # Requires modification interface
        "custom",       # Requires custom input
        "input",        # Requires text input field
    }

    def __init__(self, unsupported_actions_str: str | None = None):
        """
        Initialize permission handler.

        Args:
            unsupported_actions_str: Comma or pipe-separated list of unsupported
                                     action types (e.g., "comment,edit" or "comment|edit")
                                     If None, uses default set
        """
        # Map: chat_id -> PendingPermission
        self._pending: dict[int, PendingPermission] = {}
        # Map: chat_id -> (tool_name, tool_args) for recently approved tools
        # Used by renderer to format tool output with parameters
        self._last_approved: dict[int, tuple[str, dict]] = {}

        # Parse unsupported actions from config or use defaults
        self._unsupported_actions = self._parse_unsupported_actions(unsupported_actions_str)
        logger.info(
            f"PermissionHandler initialized with unsupported actions: "
            f"{sorted(self._unsupported_actions)}"
        )
        logger.debug(f"Received unsupported_actions_str: '{unsupported_actions_str}'")

    def _parse_unsupported_actions(self, actions_str: str | None) -> set[str]:
        """
        Parse unsupported actions string into a set.

        Supports both comma and pipe delimiters, e.g.:
        - "comment,edit,idle,turn,all"
        - "comment|edit|idle|turn|all"

        Args:
            actions_str: String with comma or pipe-separated action types

        Returns:
            Set of action type strings
        """
        if not actions_str:
            return self._DEFAULT_UNSUPPORTED_ACTIONS.copy()

        # Split on both comma and pipe, and strip whitespace
        actions = [a.strip() for a in actions_str.replace('|', ',').split(',')]

        # Filter out empty strings
        actions_set = {a for a in actions if a}

        if not actions_set:
            logger.warning(
                f"Unsupported actions string was empty after parsing, using defaults"
            )
            return self._DEFAULT_UNSUPPORTED_ACTIONS.copy()

        return actions_set

    def create_permission_ui(
        self,
        event: PermissionRequestedEvent,
        chat_id: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        """
        Create permission request UI from event.

        Args:
            event: Permission request event from jaato
            chat_id: Telegram chat ID

        Returns:
            Tuple of (message_text, inline_keyboard)
        """
        # Build message text
        lines = ["🔐 <b>Permission Request</b>\n"]

        # Tool name and description
        lines.append(f"<b>Tool:</b> <code>{event.tool_name}</code>\n")

        # Tool arguments (if any) - one per line, ellipsized
        if event.tool_args:
            lines.append("<b>Parameters:</b>")
            param_lines = format_tool_params(event.tool_args, max_width=40)
            lines.extend(param_lines)
            lines.append("")

        # Prompt from agent (why it needs permission)
        # Note: PermissionInputModeEvent doesn't have prompt_lines
        # That's only in PermissionRequestedEvent which comes earlier
        # if event.prompt_lines:
        #     lines.append("<b>Reason:</b>")
        #     for line in event.prompt_lines:
        #         lines.append(f"  {line}")
        #     lines.append("")

        # Warning (if any)
        # Note: PermissionInputModeEvent doesn't have warnings
        # That's only in PermissionRequestedEvent which comes earlier
        # if event.warnings:
        #     warning_emoji = {
        #         "info": "ℹ️",
        #         "warning": "⚠️",
        #         "danger": "🚨",
        #     }.get(event.warning_level or "warning", "⚠️")
        #     lines.append(f"{warning_emoji} <b>{event.warnings}</b>\n")

        lines.append("<i>Choose an action below:</i>")

        message_text = "\n".join(lines)

        # Create inline keyboard from response options
        keyboard = self._create_keyboard(
            event.response_options,
            event.request_id
        )

        return message_text, keyboard

    def _create_keyboard(
        self,
        options: list[dict[str, str]],
        request_id: str,
    ) -> InlineKeyboardMarkup:
        """
        Create inline keyboard from response options.

        Filters out unsupported action types that don't work well
        with Telegram's simple inline keyboard buttons.

        Args:
            options: List of response option dicts from jaato
            request_id: Permission request ID for callback data

        Returns:
            InlineKeyboardMarkup with buttons
        """
        builder = InlineKeyboardBuilder()

        # Log all incoming options at DEBUG level
        logger.debug(
            f"Creating permission keyboard with {len(options)} options: "
            f"{[{'key': opt.get('key', '?'), 'action': opt.get('action', '?'), 'label': opt.get('label', '?')} for opt in options]}"
        )
        logger.debug(f"Filtering unsupported actions: {sorted(self._unsupported_actions)}")

        # Filter out unsupported action types
        filtered_options = [
            opt for opt in options
            if opt.get("action", "") not in self._unsupported_actions
        ]

        logger.debug(f"After filtering: {len(filtered_options)} options remain")

        # If all options were filtered, log a warning
        # This can happen if the server only provides unsupported action types
        if len(filtered_options) < len(options):
            filtered_count = len(options) - len(filtered_options)
            logger.info(
                f"Filtered out {filtered_count} unsupported permission option(s) "
                f"(Telegram inline keyboard limitation): "
                f"{[opt.get('action', 'unknown') for opt in options if opt.get('action', '') in self._unsupported_actions]}"
            )

        # If no valid options remain, show a default allow/deny
        if not filtered_options:
            logger.warning(
                f"No supported permission options available, adding default yes/no buttons"
            )
            filtered_options = [
                {"key": "yes", "label": "Allow", "action": "yes"},
                {"key": "no", "label": "Deny", "action": "no"},
            ]

        for option in filtered_options:
            key = option.get("key", "")
            label = option.get("label", key)
            action = option.get("action", "unknown")

            # Add emoji based on action
            emoji = {
                "yes": "✅",
                "no": "❌",
                "always_allow": "🔄",
                "always_deny": "🚫",
            }.get(action, "▶️")

            button_label = f"{emoji} {label}"

            # Callback data format: perm:request_id:option_key
            callback_data = f"perm:{request_id}:{key}"

            builder.add(InlineKeyboardButton(
                text=button_label,
                callback_data=callback_data,
            ))

        # Layout: 2 buttons per row for good mobile display
        # This ensures button text is visible and tappable
        builder.adjust(2)

        return builder.as_markup()

    def store_pending(
        self,
        request_id: str,
        event: PermissionRequestedEvent,
        chat_id: int,
        message_id: int,
    ) -> None:
        """
        Store a pending permission request.

        Args:
            request_id: Permission request ID
            event: Permission request event (can be PermissionRequestedEvent or PermissionInputModeEvent)
            chat_id: Telegram chat ID
            message_id: Telegram message ID to edit when resolved
        """
        # PermissionInputModeEvent doesn't have prompt_lines, warnings, or warning_level
        # Use getattr with defaults to handle both event types
        self._pending[chat_id] = PendingPermission(
            request_id=request_id,
            tool_name=event.tool_name,
            tool_args=event.tool_args,
            prompt_lines=getattr(event, 'prompt_lines', None),
            warnings=getattr(event, 'warnings', None),
            warning_level=getattr(event, 'warning_level', None),
            response_options=event.response_options,
            chat_id=chat_id,
            message_id=message_id,
        )
        logger.info(
            f"Stored pending permission: request_id={request_id}, "
            f"chat_id={chat_id}, tool={event.tool_name}"
        )

    def get_pending(self, chat_id: int) -> PendingPermission | None:
        """Get pending permission for chat."""
        return self._pending.get(chat_id)

    def store_approved(self, chat_id: int, tool_name: str, tool_args: dict) -> None:
        """
        Store info about an approved tool for the renderer.
        
        The renderer can use this to format tool output with the tool name
        and parameters even if the SDK doesn't include them in output events.
        """
        self._last_approved[chat_id] = (tool_name, tool_args)
        logger.debug(f"Stored approved tool for chat_id={chat_id}: {tool_name}")

    def get_last_approved(self, chat_id: int) -> tuple[str, dict] | None:
        """
        Get the last approved tool info for this chat.
        
        Returns (tool_name, tool_args) or None if not available.
        """
        return self._last_approved.get(chat_id)

    def clear_approved(self, chat_id: int) -> None:
        """Clear the last approved tool info for this chat."""
        if chat_id in self._last_approved:
            del self._last_approved[chat_id]

    def remove_pending(self, chat_id: int) -> None:
        """Remove pending permission for chat."""
        if chat_id in self._pending:
            del self._pending[chat_id]
            logger.info(f"Removed pending permission for chat_id={chat_id}")

    def parse_callback_data(
        self,
        callback_data: str,
    ) -> tuple[str, str] | None:
        """
        Parse permission callback data.

        Args:
            callback_data: Callback data from button click

        Returns:
            Tuple of (request_id, option_key) or None if invalid format
        """
        parts = callback_data.split(":")
        if len(parts) != 3 or parts[0] != "perm":
            logger.warning(f"Invalid callback data format: {callback_data}")
            return None

        request_id = parts[1]
        option_key = parts[2]

        return request_id, option_key
