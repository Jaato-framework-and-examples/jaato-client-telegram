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
        "allow-comment", # Requires free-form text input
        "edit",         # Requires editing existing text
        "modify",       # Requires modification interface
        "custom",       # Requires custom input
        "input",        # Requires text input field
    }

    # Param-rendering thresholds (so the user can REVIEW what they approve).
    _PARAM_INLINE_MAX = 80      # single-line value up to this → inline
    _PARAM_EXPAND_MAX = 2800    # longer value up to this → in-message expandable
    _PARAM_MSG_BUDGET = 3400    # total expandable chars per message before overflow→file

    def __init__(
        self,
        unsupported_actions_str: str | None = None,
        primary_actions_str: str | None = "yes,no,always,never",
    ):
        """
        Initialize permission handler.

        Args:
            primary_actions_str: Comma/pipe-separated labels to SHOW as buttons
                (declutter). Empty => show all (legacy denylist behavior).
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
        # Labels to show as buttons (declutter). Empty => show all (legacy).
        self._primary_labels = {
            a.strip().lower()
            for a in (primary_actions_str or "").replace("|", ",").split(",")
            if a.strip()
        }
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

    def _render_params(
        self, tool_name: str, tool_args: dict[str, Any],
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Render tool params so the user can REVIEW what they're approving.

        Short scalars stay inline; long/multi-line values (e.g. ``code``) go in a
        collapsed-by-default ``<blockquote expandable>`` — full content on one tap,
        without a wall of text in the prompt. Anything too large for a Telegram
        message is sent as a file (and previewed). Returns ``(lines, files)`` where
        ``files = [(filename, content)]`` for the caller to send BEFORE the prompt.
        """
        lines: list[str] = ["<b>Parameters:</b>"]
        files: list[tuple[str, str]] = []
        budget = self._PARAM_MSG_BUDGET
        for key, value in (tool_args or {}).items():
            s = strip_ansi_codes(str(value))
            k = html.escape(str(key), quote=False)
            if "\n" not in s and len(s) <= self._PARAM_INLINE_MAX:
                lines.append(f"  • <code>{k}</code>: {html.escape(s, quote=False)}")
            elif len(s) <= self._PARAM_EXPAND_MAX and len(s) <= budget:
                budget -= len(s)
                lines.append(f"  • <code>{k}</code>:")
                lines.append(f"<blockquote expandable>{html.escape(s, quote=False)}</blockquote>")
            else:
                fname = f"{tool_name}.{key}.txt"
                files.append((fname, s))
                preview = html.escape(s[:180], quote=False)
                lines.append(
                    f"  • <code>{k}</code>: <i>({len(s)} chars — full value sent "
                    f"as {html.escape(fname)} ⬆️)</i>"
                )
                lines.append(f"<blockquote expandable>{preview}…</blockquote>")
        return lines, files

    def create_permission_ui(
        self,
        event: PermissionRequestedEvent,
        chat_id: int,
    ) -> tuple[str, InlineKeyboardMarkup, list[tuple[str, str]]]:
        """
        Create permission request UI from event.

        Args:
            event: Permission request event from jaato
            chat_id: Telegram chat ID

        Returns:
            (message_text, inline_keyboard, overflow_files) — overflow_files are
            oversized param values to send as documents before the prompt.
        """
        # Build message text
        lines = ["🔐 <b>Permission Request</b>\n"]

        # Tool name and description
        lines.append(f"<b>Tool:</b> <code>{html.escape(str(event.tool_name), quote=False)}</code>\n")

        # Tool arguments rendered for review (full content / expandable / file).
        overflow_files: list[tuple[str, str]] = []
        if event.tool_args:
            param_lines, overflow_files = self._render_params(event.tool_name, event.tool_args)
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

        return message_text, keyboard, overflow_files

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
            f"{[{'key': opt.get('short', '?'), 'full': opt.get('full', '?'), 'decision': opt.get('decision', '?')} for opt in options]}"
        )
        logger.debug(f"Filtering unsupported actions: {sorted(self._unsupported_actions)}")

        # Show only the PRIMARY actions, by LABEL. The server offers many duration
        # variants (turn/idle/once/all/comment) that clutter the prompt — and the
        # old filter keyed off `action`, which is ABSENT on the wire (options carry
        # only key/label), so it filtered nothing. label/key ARE present, so we
        # filter on those. Empty _primary_labels => legacy (drop unsupported-action
        # options only). The default-yes/no fallback below covers an empty result.
        if self._primary_labels:
            filtered_options = [
                opt for opt in options
                if (opt.get("label") or opt.get("key", "")).strip().lower()
                in self._primary_labels
            ]
        else:
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
                {"key": "y", "label": "yes", "action": "allow_once"},
                {"key": "n", "label": "no", "action": "deny"},
            ]

        for option in filtered_options:
            # Field names per PermissionResponseOption: key/label/action.
            # (Was reading short/full — stale schema — which made callback_data
            # carry an EMPTY key, so the server could never match the response
            # and every approval was effectively a no-op / deny.)
            key = option.get("key", "")
            label = option.get("label") or option.get("description") or key

            # Emoji by key (y=allow, n=deny, a=always, t=turn); action is not
            # always present on the wire so we don't key the emoji off it.
            emoji = {
                "y": "✅",
                "n": "❌",
                "a": "🔄",
                "t": "▶️",
            }.get(key, "▶️")

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
