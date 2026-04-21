# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- JAATO_TRACE_LOG environment variable support (jaato-sdk client standard)
- File-based logging when JAATO_TRACE_LOG is set and non-empty
- Console message indicating log file location when using file logging
- Configurable unsupported permission actions via `permissions.unsupported_actions` setting
- Support for both comma and pipe delimiters in action type configuration
- Environment variable support `JAATO_PERMISSION_UNSUPPORTED_ACTIONS`
- Automatic fallback to default "Allow/Deny" buttons when all options are filtered
- Abuse protection system with suspicious activity detection
- User reputation tracking and escalation
- Temporary and permanent ban management
- Admin commands: /ban, /unban, /abuse_stats
- Minimal bot-layer telemetry system
- Telegram delivery metrics tracking
- UI interaction metrics tracking
- Session pool health metrics
- Rate limiting and abuse protection metrics
- End-to-end latency metrics (avg, P50, P95, P99)
- `/telemetry` admin command for viewing metrics
- Rate limiting per user using token bucket algorithm
- Configurable per-minute and per-hour message limits
- Admin bypass option for trusted users
- Cooldown behavior with progressive penalties for repeated violations
- Rate limit statistics tracking per user
- `/rate_limit_status` command to check current rate limit status
- `/rate_limit_reset <user_id>` admin command to reset user's rate limit
- `/rate_limit_stats` admin command to view all tracked users
- Automatic cleanup of old rate limit and telemetry states
- Background task for periodic state cleanup

### Changed
- Logging configuration now respects JAATO_TRACE_LOG standard
- PermissionHandler now accepts configurable unsupported actions
- Moved hardcoded unsupported actions to configuration
- Updated bot.py to pass permission config to PermissionHandler
- Updated config.example.yaml with permissions section
- Abuse protection integrates with rate limiting for layered defense
- Updated message handlers to check abuse protection before processing
- Updated message handlers to check rate limits before processing
- Updated bot configuration to include rate limiting and abuse protection settings
- Updated admin handlers to support rate limit and abuse management
- Reduced verbose logging: permission keyboard details now DEBUG level
- Reduced verbose logging: permission UI events now DEBUG level
- Reduced verbose logging: session_id tracking now DEBUG level
- Suppressed aiogram polling logs (set to WARNING level) for cleaner output
- Added telemetry integration to bot.py and handlers

## [0.1.0] - 2025-02-20

### Added

#### Core Features
- Text-only messaging between Telegram and jaato AI agent
- Per-user session isolation with dedicated workspaces
- Progressive response streaming with edit-in-place updates
- Long message splitting at paragraph boundaries
- Polling and webhook modes for production deployment

#### Session Management
- Session pool with configurable maximum concurrent clients
- Automatic idle session cleanup
- Per-user `.env` and `.jaato/` workspace isolation

#### Permission System
- Permission approval via inline keyboards
- Tool parameter display with ellipsized formatting
- Permission state tracking across turns

#### Group Chat Support
- Mention-based triggering (@username)
- Trigger prefix support (e.g., `!ask`)
- Per-user session isolation within groups
- Group-specific help messages

#### Access Control
- Username-based whitelist system
- Admin commands for whitelist management
- Runtime whitelist reload without restart
- Silent blocking of non-whitelisted users

#### Rendering
- Expandable blockquotes for wide content (JSON, code, tables)
- Presentation context sent to jaato for output adaptation
- HTML formatting support
- Typing indicator feedback

#### Commands
- `/start` - Initialize session and connect to jaato
- `/reset` - Clear session state
- `/status` - Show session information
- `/help` - Display usage instructions

#### Admin Commands
- `/whitelist_add @user` - Add user to whitelist
- `/whitelist_remove @user` - Remove user from whitelist
- `/whitelist_list` - List all whitelisted users
- `/whitelist_reload` - Reload whitelist from file
- `/whitelist_status` - Show whitelist status

#### Bot Lifecycle
- Graceful shutdown handling
- Lifecycle event handlers for group join/leave
- Docker container status detection

### Technical Details
- Built with aiogram 3.x for async Telegram bot framework
- Uses jaato-sdk for IPC communication with jaato server
- Pydantic v2 for configuration validation
- Structlog for structured logging
- Python 3.10+ support

[0.1.0]: https://github.com/Jaato-framework-and-examples/jaato-client-telegram/releases/tag/v0.1.0
