"""
Entry point for jaato-client-telegram.

Run with: python -m jaato_client_telegram
Or via the installed script: jaato-tg
"""

import argparse
import asyncio
import logging
import os
import signal
import sys

import structlog

from jaato_client_telegram.bot import create_bot_and_dispatcher
from jaato_client_telegram.config import load_config


def _configure_logging(config) -> None:
    """
    Configure structured logging based on config settings and JAATO_TRACE_LOG standard.

    Per jaato-sdk client standard:
    - If JAATO_TRACE_LOG env var is set and non-empty, logs go to that file
    - If JAATO_TRACE_LOG is set, only print a console message about log file location
    - If JAATO_TRACE_LOG is not set, logs go to console
    """
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)
    trace_log_file = os.environ.get("JAATO_TRACE_LOG", "")

    if trace_log_file:
        # JAATO_TRACE_LOG standard: logs go to file, only print console message
        print(f"📝 Logs are being written to: {trace_log_file}", file=sys.stderr)

        # Configure file handler
        file_handler = logging.FileHandler(trace_log_file)
        file_handler.setLevel(log_level)

        if config.logging.format == "structured":
            # Structured JSON logging to file
            structlog.configure(
                processors=[
                    structlog.stdlib.filter_by_level,
                    structlog.stdlib.add_logger_name,
                    structlog.stdlib.add_log_level,
                    structlog.stdlib.PositionalArgumentsFormatter(),
                    structlog.processors.TimeStamper(fmt="iso"),
                    structlog.processors.StackInfoRenderer(),
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
                wrapper_class=structlog.stdlib.BoundLogger,
                context_class=dict,
                logger_factory=structlog.stdlib.LoggerFactory(),
                cache_logger_on_first_use=True,
            )
            logging.basicConfig(
                format="%(message)s",
                level=log_level,
                handlers=[file_handler],
            )
        else:
            # Plain text logging to file
            logging.basicConfig(
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                level=log_level,
                handlers=[file_handler],
            )
    else:
        # No JAATO_TRACE_LOG: logs go to console
        if config.logging.format == "structured":
            # Structured JSON logging (default)
            structlog.configure(
                processors=[
                    structlog.stdlib.filter_by_level,
                    structlog.stdlib.add_logger_name,
                    structlog.stdlib.add_log_level,
                    structlog.stdlib.PositionalArgumentsFormatter(),
                    structlog.processors.TimeStamper(fmt="iso"),
                    structlog.processors.StackInfoRenderer(),
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
                wrapper_class=structlog.stdlib.BoundLogger,
                context_class=dict,
                logger_factory=structlog.stdlib.LoggerFactory(),
                cache_logger_on_first_use=True,
            )
            logging.basicConfig(
                format="%(message)s",
                level=log_level,
            )
        else:
            # Plain text logging
            logging.basicConfig(
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                level=log_level,
            )

        # Suppress verbose aiogram polling logs at INFO level
        # Set aiogram to WARNING level to reduce noise
        logging.getLogger("aiogram").setLevel(logging.WARNING)
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def _idle_session_cleanup_task(pool, interval_minutes: int):
    """Background task to cleanup idle sessions periodically."""
    while True:
        try:
            await asyncio.sleep(interval_minutes * 60)
            cleaned = await pool.cleanup_idle(max_idle_minutes=interval_minutes * 2)
            if cleaned > 0:
                logging.info(f"Cleaned up {cleaned} idle sessions")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.exception(f"Error in idle session cleanup: {e}")


async def run(config_path: str | None, whitelist_path: str | None = None) -> None:
    """
    Main async run loop for the Telegram bot.

    Args:
        config_path: Optional path to config file
        whitelist_path: Optional path to whitelist JSON file
    """
    # Load configuration
    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    # Configure logging
    _configure_logging(config)
    log = logging.getLogger(__name__)

    log.info("Starting jaato-client-telegram v0.1.0")

    # Create bot and dispatcher
    bot, dp = create_bot_and_dispatcher(config, whitelist_path)

    # Add a handler for unhandled updates (for debugging)
    @dp.update()
    async def handle_unhandled_update(event):
        """Log any updates that aren't handled by registered handlers."""
        log.debug(
            f"Unhandled update: update_id={event.update_id}, "
            f"type={event.type if hasattr(event, 'type') else 'unknown'}, "
            f"chat_id={event.chat.id if hasattr(event, 'chat') and event.chat else 'N/A'}"
        )
        # We intentionally don't respond - this is just for debugging

    # Get shared dependencies
    pool = dp["pool"]

    # Start background idle session cleanup
    cleanup_task = asyncio.create_task(
        _idle_session_cleanup_task(pool, interval_minutes=30)
    )

    # Shutdown handler
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        log.info(f"Received signal {sig}, initiating graceful shutdown...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start bot in polling or webhook mode
    mode = config.telegram.mode
    log.info(f"Starting bot in {mode} mode")

    try:
        if mode == "polling":
            await dp.start_polling(bot)
        elif mode == "webhook":
            # Webhook mode for production deployment
            webhook_config = config.telegram.webhook
            if not webhook_config:
                raise ValueError("webhook configuration is required when mode=webhook")

            log.info(
                f"Setting up webhook: {webhook_config.url} "
                f"(host={webhook_config.host}, port={webhook_config.port}, "
                f"path={webhook_config.path})"
            )

            # Set webhook on startup
            await bot.set_webhook(
                url=webhook_config.url,
                secret_token=webhook_config.secret_token,
                certificate=webhook_config.cert_path,
                max_connections=webhook_config.max_connections,
                allowed_updates=webhook_config.allowed_updates,
                drop_pending_updates=True,
            )
            log.info("Webhook registered successfully")

            # Start webhook server
            await dp.start_webhook(
                bot=bot,
                webhook_path=webhook_config.path,
                host=webhook_config.host,
                port=webhook_config.port,
                secret_token=webhook_config.secret_token,
            )
        else:
            log.error(f"Unknown mode: {mode}")
            sys.exit(1)

    except Exception as e:
        log.exception(f"Fatal error running bot: {e}")
        raise

    finally:
        log.info("Shutting down...")

        # Cancel background cleanup task
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

        # Shutdown session pool (disconnect all SDK clients)
        await pool.shutdown()

        # Cleanup based on mode
        if mode == "polling":
            await dp.stop_polling()
        elif mode == "webhook":
            # Delete webhook on shutdown
            try:
                await bot.delete_webhook()
                log.info("Webhook deleted")
            except Exception as e:
                log.warning(f"Failed to delete webhook: {e}")
            await dp.stop_webhook()

        await bot.session.close()

        log.info("Shutdown complete")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="jaato-client-telegram: Telegram bot for jaato AI agent"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: jaato-client-telegram.yaml)",
    )
    parser.add_argument(
        "--whitelist",
        type=str,
        default=None,
        help="Path to whitelist JSON file (default: whitelist.json)",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run(args.config, args.whitelist))
    except KeyboardInterrupt:
        print("\nShutdown requested")
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
