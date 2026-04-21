"""
Admin command handlers for whitelist, rate limit, abuse protection, and telemetry management.

Provides commands for managing the user whitelist:
/whitelist_add @username - Add a user to the whitelist
/whitelist_remove @username - Remove a user from the whitelist
/whitelist_list - List all whitelisted users
/whitelist_reload - Reload whitelist from file

Provides commands for rate limiting:
/rate_limit_status - Show rate limit statistics
/rate_limit_reset <user_id> - Reset rate limit for a user
/rate_limit_stats - Show all tracked users
"""

import logging
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from jaato_client_telegram.whitelist import WhitelistManager

if TYPE_CHECKING:
    from jaato_client_telegram.rate_limiter import RateLimiter


logger = logging.getLogger(__name__)

router = Router()


def _get_username(message: Message) -> str | None:
    """Extract username from message sender."""
    if message.from_user:
        return message.from_user.username
    return None


@router.callback_query(lambda c: c.data.startswith("whitelist_"))
async def handle_whitelist_callback(
    callback_query: CallbackQuery,
    whitelist: WhitelistManager,
) -> None:
    """
    Handle whitelist inline keyboard button clicks.
    
    Processes approve/reject button clicks from access request notifications.
    """
    # Parse callback data
    # Format: whitelist_approve_<user_id> or whitelist_reject_<user_id>
    parts = callback_query.data.split("_")
    
    if len(parts) != 3:
        await callback_query.answer("Invalid callback data")
        return
    
    action = parts[1]  # "approve" or "reject"
    
    try:
        user_id = int(parts[2])
    except ValueError:
        await callback_query.answer("Invalid user ID")
        return
    
    # Get admin username
    admin_username = callback_query.from_user.username if callback_query.from_user else None
    
    # Verify user is admin
    if not whitelist.is_admin(admin_username):
        await callback_query.answer("⛔ You don't have permission to approve requests")
        await callback_query.message.edit_text(
            f"{callback_query.message.text}\n\n"
            f"❌ Error: @{admin_username} is not an admin"
        )
        return
    
    # Process the action
    try:
        if action == "approve":
            approved_username, approved_by = whitelist.approve_request(
                user_id, admin_username or "system"
            )
            
            # Update the notification message
            await callback_query.message.edit_text(
                f"✅ Access request approved!\n\n"
                f"User: @{approved_username}\n"
                f"Approved by: @{approved_by}\n\n"
                f"They can now use the bot."
            )
            await callback_query.answer("✅ Approved")
            
            # Notify the user (if we have their chat_id)
            request = whitelist.find_request_by_user_id(user_id)
            if request and callback_query.bot:
                try:
                    await callback_query.bot.send_message(
                        chat_id=request.chat_id,
                        text="✅ Your access request has been approved!\n\n"
                        f"You can now use this bot. Try sending a message!"
                    )
                except Exception as e:
                    logger.warning(f"Could not notify user about approval: {e}")
            
        elif action == "reject":
            request = whitelist.reject_request(user_id)
            username_part = f"@{request.username}" if request.username else f"User {request.user_id}"
            
            # Update the notification message
            await callback_query.message.edit_text(
                f"❌ Access request rejected.\n\n"
                f"User: {username_part}\n"
                f"Rejected by: @{admin_username}"
            )
            await callback_query.answer("❌ Rejected")
            
            # Optionally notify the user
            if request and callback_query.bot:
                try:
                    await callback_query.bot.send_message(
                        chat_id=request.chat_id,
                        text="❌ Your access request has been rejected."
                    )
                except Exception as e:
                    logger.warning(f"Could not notify user about rejection: {e}")
    
    except ValueError as e:
        await callback_query.answer(f"❌ Error: {e}")
        await callback_query.message.edit_text(
            f"{callback_query.message.text}\n\n"
            f"❌ {e}"
        )
    except Exception as e:
        logger.exception(f"Error processing whitelist callback: {e}")
        await callback_query.answer("❌ An error occurred")


@router.message(Command("whitelist_add"))
async def cmd_whitelist_add(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    Add a user to the whitelist.

    Usage: /whitelist_add @username

    Only admins can use this command.
    """
    username = _get_username(message)

    # Check if sender is admin
    if not whitelist.is_admin(username):
        await message.answer(
            "⛔ You don't have permission to manage the whitelist.\n\n"
            "Only administrators can use this command."
        )
        return

    # Parse command arguments
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Usage: /whitelist_add @username\n\n"
            "Example: /whitelist_add @alice"
        )
        return

    target_username = args[1].strip()

    # Add to whitelist
    try:
        whitelist.add_user(target_username, username or "system")
        await message.answer(
            f"✅ Added @{target_username} to the whitelist.\n\n"
            f"They can now use the bot."
        )
    except ValueError as e:
        await message.answer(f"❌ {e}")


@router.message(Command("whitelist_remove"))
async def cmd_whitelist_remove(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    Remove a user from the whitelist.

    Usage: /whitelist_remove @username

    Only admins can use this command.
    """
    username = _get_username(message)

    # Check if sender is admin
    if not whitelist.is_admin(username):
        await message.answer(
            "⛔ You don't have permission to manage the whitelist.\n\n"
            "Only administrators can use this command."
        )
        return

    # Parse command arguments
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Usage: /whitelist_remove @username\n\n"
            "Example: /whitelist_remove @alice"
        )
        return

    target_username = args[1].strip()

    # Remove from whitelist
    try:
        whitelist.remove_user(target_username)
        await message.answer(
            f"✅ Removed @{target_username} from the whitelist.\n\n"
            f"They can no longer use the bot."
        )
    except ValueError as e:
        await message.answer(f"❌ {e}")


@router.message(Command("whitelist_list"))
async def cmd_whitelist_list(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    List all whitelisted users.

    Anyone can use this command to see who has access.
    """
    users = whitelist.list_users()

    if not users:
        await message.answer(
            "📋 Whitelist is empty.\n\n"
            "No users are currently authorized to use the bot."
        )
        return

    # Format user list
    user_list = "\n".join(f"• @{u}" for u in sorted(users))

    await message.answer(
        f"📋 Whitelisted Users ({len(users)}):\n\n"
        f"{user_list}"
    )


@router.message(Command("whitelist_reload"))
async def cmd_whitelist_reload(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    Reload whitelist from file.

    Usage: /whitelist_reload

    Only admins can use this command.
    """
    username = _get_username(message)

    # Check if sender is admin
    if not whitelist.is_admin(username):
        await message.answer(
            "⛔ You don't have permission to manage the whitelist.\n\n"
            "Only administrators can use this command."
        )
        return

    # Reload whitelist
    try:
        whitelist.reload()
        users = whitelist.list_users()
        await message.answer(
            f"✅ Whitelist reloaded from file.\n\n"
            f"Current users: {len(users)}"
        )
    except Exception as e:
        await message.answer(f"❌ Failed to reload whitelist: {e}")


@router.message(Command("whitelist_status"))
async def cmd_whitelist_status(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    Show whitelist status.

    Shows whether whitelist is enabled and how many users are whitelisted.
    """
    username = _get_username(message)
    users = whitelist.list_users()
    is_allowed = whitelist.is_allowed(username)
    is_admin = whitelist.is_admin(username)

    status_lines = [
        "🔒 Whitelist Status\n",
        f"Enabled: {'✅ Yes' if whitelist.config.enabled else '❌ No'}",
        f"Total users: {len(users)}",
        "",
    ]

    if username:
        status_lines.extend([
            f"Your username: @{username}",
            f"Your access: {'✅ Allowed' if is_allowed else '❌ Not allowed'}",
            f"Admin: {'✅ Yes' if is_admin else '❌ No'}",
        ])
    else:
        status_lines.append(
            "⚠️ You don't have a Telegram username. "
            "Please set one in Telegram settings."
        )

    await message.answer("\n".join(status_lines))


@router.message(Command("requests"))
async def cmd_requests_list(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    List pending access requests.

    Shows all users who have requested access and are waiting for approval.
    Only admins can use this command.
    """
    username = _get_username(message)

    # Check if sender is admin
    if not whitelist.is_admin(username):
        await message.answer(
            "⛔ You don't have permission to view access requests.\n\n"
            "Only administrators can use this command."
        )
        return

    # Get pending requests
    pending = whitelist.get_pending_requests()

    if not pending:
        await message.answer(
            "📋 No pending access requests.\n\n"
            "All caught up!"
        )
        return

    # Format request list
    lines = [f"📋 Pending Access Requests ({len(pending)}):\n"]

    for i, req in enumerate(pending, 1):
        username_part = f"@{req.username}" if req.username else "No username"
        name_part = f"{req.first_name or ''} {req.last_name or ''}".strip()
        lines.append(
            f"{i}. {username_part} ({name_part})"
            f"\n   User ID: {req.user_id}"
            f"\n   Requested: {req.requested_at}"
        )
        if req.message:
            lines.append(f"   Message: {req.message}")
        lines.append("")

    # Split into chunks if too long (Telegram message limit)
    chunk_size = 3
    for i in range(0, len(lines), chunk_size):
        chunk = "\n".join(lines[i:i + chunk_size])
        await message.answer(chunk)


@router.message(Command("approve"))
async def cmd_approve_request(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    Approve an access request.

    Usage: /approve <user_id>

    Only admins can use this command.
    """
    username = _get_username(message)

    # Check if sender is admin
    if not whitelist.is_admin(username):
        await message.answer(
            "⛔ You don't have permission to approve requests.\n\n"
            "Only administrators can use this command."
        )
        return

    # Parse command arguments
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Usage: /approve <user_id>\n\n"
            "Example: /approve 123456789\n\n"
            "Use /requests to see pending requests and their user IDs."
        )
        return

    # Parse user_id
    try:
        user_id = int(args[1].strip())
    except ValueError:
        await message.answer(
            "❌ Invalid user ID. Must be a number.\n\n"
            "Example: /approve 123456789"
        )
        return

    # Approve request
    try:
        approved_username, approved_by = whitelist.approve_request(
            user_id, username or "system"
        )
        await message.answer(
            f"✅ Access request approved!\n\n"
            f"User: @{approved_username}\n"
            f"Approved by: @{approved_by}\n\n"
            f"They can now use the bot."
        )
        logger.info(f"Access request approved for @{approved_username} by @{approved_by}")
    except ValueError as e:
        await message.answer(f"❌ {e}")


@router.message(Command("reject"))
async def cmd_reject_request(
    message: Message,
    whitelist: WhitelistManager,
) -> None:
    """
    Reject an access request.

    Usage: /reject <user_id>

    Only admins can use this command.
    """
    username = _get_username(message)

    # Check if sender is admin
    if not whitelist.is_admin(username):
        await message.answer(
            "⛔ You don't have permission to reject requests.\n\n"
            "Only administrators can use this command."
        )
        return

    # Parse command arguments
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Usage: /reject <user_id>\n\n"
            "Example: /reject 123456789\n\n"
            "Use /requests to see pending requests and their user IDs."
        )
        return

    # Parse user_id
    try:
        user_id = int(args[1].strip())
    except ValueError:
        await message.answer(
            "❌ Invalid user ID. Must be a number.\n\n"
            "Example: /reject 123456789"
        )
        return

    # Reject request
    try:
        request = whitelist.reject_request(user_id)
        username_part = f"@{request.username}" if request.username else f"User {request.user_id}"
        await message.answer(
            f"✅ Access request rejected.\n\n"
            f"User: {username_part}\n\n"
            f"They will not be notified."
        )
    except Exception as e:
        await message.answer(
            f"❌ Error rejecting request.\n\n"
            f"Details: {e}"
        )


@router.message(Command("rate_limit_status"))
async def cmd_rate_limit_status(
    message: Message,
    rate_limiter: "RateLimiter | None" = None,
) -> None:
    """
    Show rate limit status for the current user.

    Displays current token bucket state and limits.
    """
    if rate_limiter is None:
        await message.answer(
            "ℹ️ Rate limiting is not enabled.\n\n"
            "Enable it in your config file:\n"
            "```yaml\n"
            "rate_limiting:\n"
            "  enabled: true\n"
            "```"
        )
        return

    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        await message.answer("❌ Could not determine your user ID")
        return

    stats = await rate_limiter.get_user_stats(user_id)

    status_lines = [
        "📊 <b>Your Rate Limit Status</b>\n",
    ]

    if stats.get("is_bypassed"):
        status_lines.append("✅ <i>You are an admin - rate limits bypassed</i>\n")
    else:
        status_lines.append(
            f"<b>Minute Limit:</b> {stats['minute_available']}/{stats['minute_limit']} messages\n"
            f"<b>Hour Limit:</b> {stats['hour_available']}/{stats['hour_limit']} messages\n"
        )

    if "cooldown_remaining" in stats:
        status_lines.append(
            f"\n⏸️ <b>Cooldown:</b> {stats['cooldown_remaining']}s remaining"
        )

    status_lines.append(f"\n<b>Total messages:</b> {stats['total_messages']}")

    await message.answer("\n".join(status_lines), parse_mode="HTML")


@router.message(Command("rate_limit_reset"))
async def cmd_rate_limit_reset(
    message: Message,
    rate_limiter: "RateLimiter | None" = None,
) -> None:
    """
    Reset rate limit state for a user.

    Usage: /rate_limit_reset <user_id>

    Only admins can use this command.
    """
    if rate_limiter is None:
        await message.answer("ℹ️ Rate limiting is not enabled")
        return

    # Parse command arguments
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Usage: /rate_limit_reset <user_id>\n\n"
            "Example: /rate_limit_reset 123456789\n\n"
            "Use /rate_limit_stats to see tracked users and their IDs."
        )
        return

    # Parse user_id
    try:
        user_id = int(args[1].strip())
    except ValueError:
        await message.answer(
            "❌ Invalid user ID. Must be a number.\n\n"
            "Example: /rate_limit_reset 123456789"
        )
        return

    # Reset user's rate limit state
    await rate_limiter.reset_user(user_id)
    await message.answer(
        f"✅ Rate limit state reset for user {user_id}.\n\n"
        f"They can now send messages normally."
    )


@router.message(Command("rate_limit_stats"))
async def cmd_rate_limit_stats(
    message: Message,
    rate_limiter: "RateLimiter | None" = None,
) -> None:
    """
    Show rate limit statistics for all tracked users.

    Displays all users and their current token bucket state.
    Only admins can use this command.
    """
    if rate_limiter is None:
        await message.answer("ℹ️ Rate limiting is not enabled")
        return

    all_stats = await rate_limiter.get_all_stats()

    if not all_stats:
        await message.answer("📊 No users are currently tracked for rate limiting.")
        return

    # Sort by total messages (most active first)
    sorted_users = sorted(
        all_stats.items(),
        key=lambda x: x[1]["total_messages"],
        reverse=True,
    )

    status_lines = [
        f"📊 <b>Rate Limit Statistics</b>\n",
        f"<b>Tracked Users:</b> {len(sorted_users)}\n",
    ]

    for user_id, stats in sorted_users[:20]:  # Show top 20
        cooldown_text = ""
        if "cooldown_remaining" in stats:
            cooldown_text = f" | ⏸️ {stats['cooldown_remaining']}s"

        bypass_text = " | 👑" if stats.get("is_bypassed") else ""

        status_lines.append(
            f"\n<b>User {user_id}</b>: "
            f"{stats['total_messages']} msgs | "
            f"⏱️ {stats['minute_available']}/{stats['minute_limit']} | "
            f"🕐 {stats['hour_available']}/{stats['hour_limit']}"
            f"{cooldown_text}{bypass_text}"
        )

    if len(sorted_users) > 20:
        status_lines.append(f"\n... and {len(sorted_users) - 20} more users")

    # Handle long messages
    text = "\n".join(status_lines)
    if len(text) > 4096:
        text = text[:4000] + "\n\n... (truncated)"

    await message.answer(text, parse_mode="HTML")


@router.message(Command("ban"))
async def cmd_ban(
    message: Message,
    abuse_protector: "AbuseProtector | None" = None,
) -> None:
    """
    Ban a user (admin command).

    Usage: /ban <user_id> [reason]

    Options:
      --temp   Temporary ban (uses configured duration)
      --perm   Permanent ban (default)

    Examples:
      /ban 123456789 Spamming
      /ban 123456789 --temp Abusive behavior
      /ban 123456789 --perm TOS violation
    """
    if abuse_protector is None:
        await message.answer("ℹ️ Abuse protection is not enabled")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Usage: /ban <user_id> [reason]\n\n"
            "Options:\n"
            "  --temp   Temporary ban\n"
            "  --perm   Permanent ban (default)\n\n"
            "Examples:\n"
            "  /ban 123456789 Spamming\n"
            "  /ban 123456789 --temp Abusive behavior"
        )
        return

    # Parse arguments
    parts = args[1].strip().split()
    if not parts:
        await message.answer("❌ Invalid arguments")
        return

    # Extract user_id
    try:
        user_id = int(parts[0])
    except ValueError:
        await message.answer(f"❌ Invalid user ID: {parts[0]}")
        return

    # Check for options
    is_temp = "--temp" in parts
    is_perm = "--perm" in parts

    # Extract reason (remove options and user_id)
    reason_parts = [p for p in parts[1:] if p not in ["--temp", "--perm"]]
    reason = " ".join(reason_parts) if reason_parts else "Manual ban by admin"

    # Apply ban
    from jaato_client_telegram.abuse_protection import BanLevel

    ban_level = BanLevel.TEMPORARY if is_temp else BanLevel.PERMANENT
    await abuse_protector.ban_user(
        user_id=user_id,
        ban_level=ban_level,
        reason=reason,
    )

    level_text = "temporary" if is_temp else "permanent"
    await message.answer(
        f"✅ User {user_id} has been {level_text}ly banned.\n\n"
        f"Reason: {reason}"
    )


@router.message(Command("unban"))
async def cmd_unban(
    message: Message,
    abuse_protector: "AbuseProtector | None" = None,
) -> None:
    """
    Unban a user (admin command).

    Usage: /unban <user_id>

    Example: /unban 123456789
    """
    if abuse_protector is None:
        await message.answer("ℹ️ Abuse protection is not enabled")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Usage: /unban <user_id>\n\n"
            "Example: /unban 123456789"
        )
        return

    try:
        user_id = int(args[1].strip())
    except ValueError:
        await message.answer(f"❌ Invalid user ID: {args[1]}")
        return

    await abuse_protector.unban_user(user_id)
    await message.answer(f"✅ User {user_id} has been unbanned.")


@router.message(Command("abuse_stats"))
async def cmd_abuse_stats(
    message: Message,
    abuse_protector: "AbuseProtector | None" = None,
) -> None:
    """
    Show abuse protection statistics for all users (admin command).

    Displays all tracked users and their abuse status.
    """
    if abuse_protector is None:
        await message.answer("ℹ️ Abuse protection is not enabled")
        return

    all_stats = await abuse_protector.get_all_stats()

    if not all_stats:
        await message.answer("📊 No users are currently tracked for abuse protection.")
        return

    # Sort by suspicion score (most suspicious first)
    sorted_users = sorted(
        all_stats.items(),
        key=lambda x: x[1]["suspicion_score"],
        reverse=True,
    )

    status_lines = [
        f"📊 <b>Abuse Protection Statistics</b>\n",
        f"<b>Tracked Users:</b> {len(sorted_users)}\n",
    ]

    for user_id, stats in sorted_users[:20]:  # Show top 20
        ban_text = ""
        if stats["banned"]:
            ban_level = stats.get("ban_level", "unknown")
            ban_text = f" | 🚫 {ban_level}"

        reputation_emoji = {
            "high": "🟢",
            "medium": "🟡",
            "low": "🟠",
            "critical": "🔴",
        }.get(
            "high" if stats["reputation"] >= 70 else
            "medium" if stats["reputation"] >= 40 else
            "low" if stats["reputation"] >= 20 else
            "critical",
            "⚪"
        )

        status_lines.append(
            f"\n<b>User {user_id}</b>: "
            f"{stats['total_messages']} msgs | "
            f"suspicion={stats['suspicion_score']:.1f} | "
            f"rep={stats['reputation']:.1f}{reputation_emoji}"
            f"{ban_text}"
        )

    if len(sorted_users) > 20:
        status_lines.append(f"\n... and {len(sorted_users) - 20} more users")

    # Handle long messages
    text = "\n".join(status_lines)
    if len(text) > 4096:
        text = text[:4000] + "\n\n... (truncated)"

    await message.answer(text, parse_mode="HTML")


@router.message(Command("telemetry"))
async def cmd_telemetry(
    message: Message,
    telemetry: "TelemetryCollector | None" = None,
) -> None:
    """
    Show telemetry statistics (admin command).

    Displays bot-layer metrics collected by the telemetry system.
    """
    if telemetry is None:
        await message.answer(
            "ℹ️ Telemetry is not enabled.\n\n"
            "Enable it in your config file:\n"
            "```yaml\n"
            "telemetry:\n"
            "  enabled: true\n"
            "```"
        )
        return

    # Get telemetry summary
    summary = await telemetry.get_summary()

    # Build status message
    uptime_hours = summary["uptime_seconds"] / 3600
    status_lines = [
        "📊 <b>Telemetry Statistics</b>\n",
        f"<b>Uptime:</b> {uptime_hours:.1f} hours\n",
    ]

    # Telegram delivery metrics
    if summary.get("telegram_delivery"):
        td = summary["telegram_delivery"]
        status_lines.append(
            f"<b>📤 Telegram API:</b>\n"
            f"  Sent: {td['messages_sent']}\n"
            f"  Failed: {td['messages_failed']}\n"
            f"  Error rate: {td['error_rate']:.1%}\n"
            f"  Errors (1h): {td['errors_last_hour']}\n"
        )

    # UI interaction metrics
    if summary.get("ui_interactions"):
        ui = summary["ui_interactions"]
        status_lines.append(
            f"<b>🖱️ UI Interactions:</b>\n"
            f"  Permissions: {ui['permission_approvals']}✅ / {ui['permission_denials']}❌\n"
            f"  Message edits: {ui['message_edits']}\n"
            f"  Collapsible expands: {ui['collapsible_expands']}\n"
        )
        if ui["top_commands"]:
            top_cmds = ", ".join([f"/{cmd}" for cmd in list(ui["top_commands"].keys())[:3]])
            status_lines.append(f"  Top commands: {top_cmds}\n")

    # Session pool metrics
    if summary.get("session_pool"):
        sp = summary["session_pool"]
        status_lines.append(
            f"<b>🔗 Session Pool:</b>\n"
            f"  Active: {sp['active_connections']}/{sp['max_connections']}\n"
            f"  Utilization: {sp['utilization']:.1%}\n"
            f"  Errors: {sp['connection_errors']}\n"
            f"  Avg session: {sp['avg_session_duration']:.1f}s\n"
        )

    # Rate limiting metrics
    if summary.get("rate_limiting"):
        rl = summary["rate_limiting"]
        status_lines.append(
            f"<b>⏱️ Rate Limiting:</b>\n"
            f"  Users limited: {rl['users_limited']}\n"
            f"  Cooldowns: {rl['cooldowns_triggered']}\n"
            f"  Active limited: {rl['active_limited_users']}\n"
        )

    # Abuse protection metrics
    if summary.get("abuse_protection"):
        ap = summary["abuse_protection"]
        status_lines.append(
            f"<b>🛡️ Abuse Protection:</b>\n"
            f"  Bans applied: {ap['bans_applied']}\n"
            f"  Temporary: {ap['temporary_bans']}\n"
            f"  Permanent: {ap['permanent_bans']}\n"
            f"  Warnings: {ap['warnings_issued']}\n"
        )

    # Latency metrics
    if summary.get("latency"):
        lat = summary["latency"]
        status_lines.append(
            f"<b>⚡ Latency (end-to-end):</b>\n"
            f"  Avg: {lat['avg_latency_ms']}ms\n"
            f"  P50: {lat['p50_latency_ms']}ms\n"
            f"  P95: {lat['p95_latency_ms']}ms\n"
            f"  P99: {lat['p99_latency_ms']}ms\n"
            f"  Requests: {lat['request_count']}\n"
        )

    # Handle long messages
    text = "\n".join(status_lines)
    if len(text) > 4096:
        text = text[:4000] + "\n\n... (truncated)"

    await message.answer(text, parse_mode="HTML")
