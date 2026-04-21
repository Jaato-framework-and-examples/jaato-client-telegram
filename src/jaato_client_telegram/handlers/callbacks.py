"""
Callback query handlers for jaato-client-telegram.

Handles inline keyboard button callbacks, including permission approvals.
"""

import logging

from aiogram import Router
from aiogram.types import CallbackQuery

from jaato_client_telegram.permissions import PermissionHandler, format_tool_params
from jaato_client_telegram.session_pool import SessionPool


logger = logging.getLogger(__name__)

router = Router()


def _is_permission_callback(callback_query: CallbackQuery) -> bool:
    """Check if callback query is for permission handling."""
    if not callback_query.data:
        return False
    return callback_query.data.startswith("perm:")


@router.callback_query(_is_permission_callback)
async def handle_permission_callback(
    query: CallbackQuery,
    permission_handler: PermissionHandler,
    pool: SessionPool,
) -> None:
    """
    Handle permission approval/denial button clicks.

    Parses the callback data, sends the response to jaato via SDK,
    and updates the permission message to show the result.

    Callback data format: perm:request_id:option_key
    Example: perm:abc123:yes
    """
    if not query.data or not query.message:
        await query.answer("❌ Invalid callback")
        return

    chat_id = query.message.chat.id

    # Parse callback data
    parsed = permission_handler.parse_callback_data(query.data)
    if not parsed:
        await query.answer("❌ Invalid callback format")
        return

    request_id, option_key = parsed

    # Get pending permission
    pending = permission_handler.get_pending(chat_id)
    if not pending or pending.request_id != request_id:
        await query.answer("❌ Permission request not found or expired")
        return

    # Acknowledge the button click immediately
    await query.answer()

    # Get the label for the selected option
    option_label = option_key
    for option in pending.response_options:
        if option.get("key") == option_key:
            option_label = option.get("label", option_key)
            break

    # Update message to show decision
    action_emoji = {
        "yes": "✅",
        "no": "❌",
        "always_allow": "🔄",
        "always_deny": "🚫",
    }.get(option_key, "▶️")

    # Build result message with tool name and parameters (HTML format)
    result_lines = [
        f"{action_emoji} <b>Decision</b>: {option_label}",
        "",
        f"🔧 <code>{pending.tool_name}</code>",
    ]
    
    # Add tool parameters if available
    if pending.tool_args:
        param_lines = format_tool_params(pending.tool_args, max_width=40)
        result_lines.extend(param_lines)
    
    result_lines.extend(["", "⏳ Sending response to jaato..."])
    result_text = "\n".join(result_lines)

    try:
        await query.message.edit_text(result_text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to edit permission message: {e}")

    # Send permission response to jaato SDK
    try:
        # Get the SDK client for this user
        client = await pool.get_client(chat_id)
        
        # Call respond_to_permission
        await client.respond_to_permission(
            request_id=request_id,
            response=option_key,
            edited_arguments=None,
        )
        
        logger.info(
            f"Permission response sent: request_id={request_id}, "
            f"response={option_key}, chat_id={chat_id}"
        )
        
        # Update message to show success
        success_lines = [
            f"{action_emoji} <b>Decision</b>: {option_label}",
            "",
            f"🔧 <code>{pending.tool_name}</code>",
        ]
        
        # Add tool parameters if available
        if pending.tool_args:
            param_lines = format_tool_params(pending.tool_args, max_width=40)
            success_lines.extend(param_lines)
        
        success_lines.extend(["", "✅ Response sent to jaato"])
        success_text = "\n".join(success_lines)
        
        try:
            await query.message.edit_text(success_text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to update success message: {e}")
        
        # Store approved tool info so renderer can format output with parameters
        if option_key not in ("no", "always_deny"):
            permission_handler.store_approved(
                chat_id=chat_id,
                tool_name=pending.tool_name,
                tool_args=pending.tool_args or {},
            )
        
    except Exception as e:
        logger.error(f"Failed to send permission response: {e}")
        
        # Update message to show error with tool parameters (HTML format)
        error_lines = [
            f"{action_emoji} <b>Decision</b>: {option_label}",
            "",
            f"🔧 <code>{pending.tool_name}</code>",
        ]
        
        if pending.tool_args:
            param_lines = format_tool_params(pending.tool_args, max_width=40)
            error_lines.extend(param_lines)
        
        error_lines.extend(["", f"❌ Failed to send response: {e}"])
        error_text = "\n".join(error_lines)
        
        try:
            await query.message.edit_text(error_text, parse_mode="HTML")
        except Exception as e2:
            logger.warning(f"Failed to update error message: {e2}")

    # Remove pending permission
    permission_handler.remove_pending(chat_id)

