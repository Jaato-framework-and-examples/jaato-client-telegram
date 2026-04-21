"""
Bot setup and dispatcher configuration for jaato-client-telegram.

Creates the aiogram Bot and Dispatcher, registers all handlers,
and wires up dependencies (SessionPool, ResponseRenderer).
"""

import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from jaato_client_telegram.config import Config
from jaato_client_telegram.file_handler import FileHandler
from jaato_client_telegram.workspace_tracker import WorkspaceFileTracker
from jaato_client_telegram.agent_response_tracker import AgentResponseTracker
from jaato_client_telegram.handlers import (
    admin_router,
    callbacks_router,
    commands_router,
    group_router,
    lifecycle_router,
    private_router,
)
from jaato_client_telegram.permissions import PermissionHandler
from jaato_client_telegram.rate_limiter import RateLimiter
from jaato_client_telegram.abuse_protection import AbuseProtector
from jaato_client_telegram.telemetry import TelemetryCollector
from jaato_client_telegram.renderer import ResponseRenderer
from jaato_client_telegram.session_pool import SessionPool
from jaato_client_telegram.whitelist import WhitelistManager
from jaato_client_telegram.transport import WSTransport


logger = logging.getLogger(__name__)


def _create_renderer(config: Config, permission_handler: PermissionHandler | None = None, file_handler: FileHandler | None = None, agent_response_tracker: AgentResponseTracker | None = None, session_pool: "SessionPool | None" = None) -> ResponseRenderer:
    """Create a ResponseRenderer with config settings."""
    return ResponseRenderer(
        max_message_length=config.rendering.max_message_length,
        edit_throttle_ms=config.rendering.edit_throttle_ms,
        permission_handler=permission_handler,
        file_handler=file_handler,
        agent_response_tracker=agent_response_tracker,
        session_pool=session_pool,
    )


def _create_session_pool(config: Config, transport: WSTransport) -> SessionPool:
    """Create a SessionPool with config settings."""
    return SessionPool(
        transport=transport,
        max_concurrent=config.session.max_concurrent,
    )


def create_bot_and_dispatcher(
    config: Config, whitelist_path: str | None = None
) -> tuple[Bot, Dispatcher]:
    """
    Create and configure the Telegram bot and dispatcher.

    Args:
        config: Validated configuration object
        whitelist_path: Path to whitelist JSON file

    Returns:
        Tuple of (Bot, Dispatcher)

    Raises:
        ValueError: If bot_token is not configured
    """
    if not config.telegram.bot_token:
        raise ValueError("telegram.bot_token must be set in config")

    # Create bot instance with aiogram 3.x syntax
    bot = Bot(
        token=config.telegram.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Create dispatcher with FSM storage (for future flows like permissions)
    dp = Dispatcher(storage=MemoryStorage())

    # Create shared dependencies
    transport = WSTransport(
        url=config.jaato_ws.url,
        tls_config=config.jaato_ws.tls,
        secret_token=config.jaato_ws.secret_token,
    )
    pool = _create_session_pool(config, transport)
    permission_handler = PermissionHandler(config.permissions.unsupported_actions)
    file_handler = FileHandler(config.file_sharing)
    workspace_tracker = WorkspaceFileTracker()
    agent_response_tracker = AgentResponseTracker(workspace_tracker)

    # Note: WorkspaceEventSubscriber needs an IPC backend from jaato-server
    # This will be added once we determine the source of the IPC connection
    # For now, event_subscriber is None and won't be started

    renderer = _create_renderer(config, permission_handler, file_handler, agent_response_tracker, pool)
    whitelist = WhitelistManager(whitelist_path, bot=bot)  # Pass bot for notifications

    # Create rate limiter if enabled
    rate_limiter: RateLimiter | None = None
    if config.rate_limiting.enabled:
        rate_limiter = RateLimiter(config.rate_limiting)
        logger.info("Rate limiting enabled")

    # Create abuse protector if enabled
    abuse_protector: AbuseProtector | None = None
    if config.abuse_protection.enabled:
        abuse_protector = AbuseProtector(config.abuse_protection)
        logger.info("Abuse protection enabled")

    # Create telemetry collector if enabled
    telemetry: TelemetryCollector | None = None
    if config.telemetry.enabled:
        telemetry = TelemetryCollector(config.telemetry)
        logger.info("Telemetry enabled")

    # Register routers with dependency injection
    # Order matters: specific routers first, then general ones
    # Lifecycle router first (bot join/leave events)
    # Admin commands don't go through whitelist check
    dp.include_router(admin_router)
    dp.include_router(lifecycle_router)
    dp.include_router(commands_router)
    dp.include_router(callbacks_router)
    dp.include_router(group_router)

    # Apply whitelist middleware to private messages router
    # This creates middleware that sends polite access request messages
    # to non-whitelisted users
    whitelist_middleware = whitelist.create_middleware(silent=False)
    private_router.message.middleware(whitelist_middleware)
    dp.include_router(private_router)

    # Store dependencies in dispatcher's context data
    # This makes them available to all handlers via dependency injection
    dp["pool"] = pool
    dp["renderer"] = renderer
    dp["whitelist"] = whitelist
    dp["permission_handler"] = permission_handler
    dp["config"] = config
    dp["rate_limiter"] = rate_limiter
    dp["abuse_protector"] = abuse_protector
    dp["telemetry"] = telemetry
    dp["admin_user_ids"] = config.telegram.access.admin_user_ids
    dp["workspace_tracker"] = workspace_tracker
    dp["agent_response_tracker"] = agent_response_tracker

    logger.info(
        "Bot and dispatcher configured: "
        f"polling={config.telegram.mode == 'polling'}, "
        f"max_sessions={config.session.max_concurrent}, "
        f"whitelist_enabled={whitelist.config.enabled}, "
        f"rate_limiting_enabled={config.rate_limiting.enabled}, "
        f"workspace_tracking_enabled={workspace_tracker is not None}"
    )

    return bot, dp
