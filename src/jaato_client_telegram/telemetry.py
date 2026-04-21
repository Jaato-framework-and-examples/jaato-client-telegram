"""
Minimal bot-layer telemetry for jaato-client-telegram.

Collects bot-specific metrics that jaato-server doesn't track:
- Telegram API delivery metrics
- UI interaction metrics (buttons, commands)
- Session pool health metrics
- Rate limiting metrics
- Abuse protection metrics
- End-to-end latency metrics
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from jaato_client_telegram.config import TelemetryConfig


logger = logging.getLogger(__name__)


@dataclass
class TelegramDeliveryMetrics:
    """Telegram API delivery metrics."""
    messages_sent: int = 0
    messages_failed: int = 0
    errors_last_hour: list[str] = field(default_factory=list)
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None


@dataclass
class UIInteractionMetrics:
    """UI interaction metrics."""
    permission_approvals: int = 0
    permission_denials: int = 0
    command_usage: dict[str, int] = field(default_factory=dict)
    button_clicks: dict[str, int] = field(default_factory=dict)
    collapsible_expands: int = 0
    message_edits: int = 0


@dataclass
class SessionPoolMetrics:
    """Session pool health metrics."""
    active_connections: int = 0
    max_connections: int = 50
    connection_errors: int = 0
    disconnections: int = 0
    avg_session_duration: float = 0.0
    session_durations: deque = field(default_factory=lambda: deque(maxlen=100))


@dataclass
class RateLimitingMetrics:
    """Rate limiting metrics."""
    users_limited: int = 0
    cooldowns_triggered: int = 0
    most_limited_user: Optional[int] = None
    limit_hits_last_hour: dict[int, int] = field(default_factory=lambda: defaultdict(int))


@dataclass
class AbuseProtectionMetrics:
    """Abuse protection metrics."""
    bans_applied: int = 0
    temporary_bans: int = 0
    permanent_bans: int = 0
    warnings_issued: int = 0
    suspicious_users: int = 0
    unban_operations: int = 0


@dataclass
class LatencyMetrics:
    """End-to-end latency metrics."""
    request_count: int = 0
    total_latency: float = 0.0
    avg_latency: float = 0.0
    p50_latency: float = 0.0
    p95_latency: float = 0.0
    p99_latency: float = 0.0
    latencies: deque = field(default_factory=lambda: deque(maxlen=1000))


class TelemetryCollector:
    """
    Minimal bot-layer telemetry collector.

    Only collects metrics that jaato-server doesn't already track.
    Focuses on Telegram API, UI interactions, and client-side operations.
    """

    def __init__(self, config: TelemetryConfig) -> None:
        """
        Initialize telemetry collector.

        Args:
            config: Telemetry configuration
        """
        self._config = config
        self._start_time = time.time()
        self._lock = asyncio.Lock()

        # Per-metrics storage
        self._telegram: TelegramDeliveryMetrics = TelegramDeliveryMetrics()
        self._ui: UIInteractionMetrics = UIInteractionMetrics()
        self._session_pool: SessionPoolMetrics = SessionPoolMetrics()
        self._rate_limiting: RateLimitingMetrics = RateLimitingMetrics()
        self._abuse: AbuseProtectionMetrics = AbuseProtectionMetrics()
        self._latency: LatencyMetrics = LatencyMetrics()

        logger.info("TelemetryCollector initialized")

    # Telegram Delivery Metrics

    def record_message_sent(self) -> None:
        """Record a successful message sent to Telegram."""
        if self._config.collect_telegram_delivery:
            self._telegram.messages_sent += 1

    def record_message_failed(self, error: str) -> None:
        """Record a failed message send."""
        if self._config.collect_telegram_delivery:
            self._telegram.messages_failed += 1
            self._telegram.last_error = error
            self._telegram.last_error_time = time.time()

            # Track errors in last hour
            now = time.time()
            self._telegram.errors_last_hour = [
                e for e in self._telegram.errors_last_hour
                if now - (self._start_time + e.get("time", 0)) < 3600
            ]
            self._telegram.errors_last_hour.append({"time": now - self._start_time, "error": error})

    # UI Interaction Metrics

    def record_permission_approval(self) -> None:
        """Record a permission approval."""
        if self._config.collect_ui_interactions:
            self._ui.permission_approvals += 1

    def record_permission_denial(self) -> None:
        """Record a permission denial."""
        if self._config.collect_ui_interactions:
            self._ui.permission_denials += 1

    def record_command_usage(self, command: str) -> None:
        """Record a command usage."""
        if self._config.collect_ui_interactions:
            self._ui.command_usage[command] = self._ui.command_usage.get(command, 0) + 1

    def record_button_click(self, button_type: str) -> None:
        """Record a button click."""
        if self._config.collect_ui_interactions:
            self._ui.button_clicks[button_type] = self._ui.button_clicks.get(button_type, 0) + 1

    def record_collapsible_expand(self) -> None:
        """Record a collapsible content expand."""
        if self._config.collect_ui_interactions:
            self._ui.collapsible_expands += 1

    def record_message_edit(self) -> None:
        """Record a message edit operation."""
        if self._config.collect_ui_interactions:
            self._ui.message_edits += 1

    # Session Pool Metrics

    def record_session_created(self) -> None:
        """Record a session creation."""
        if self._config.collect_session_pool:
            self._session_pool.active_connections += 1

    def record_session_ended(self, duration: float) -> None:
        """Record a session end."""
        if self._config.collect_session_pool:
            self._session_pool.active_connections -= 1
            self._session_pool.disconnections += 1
            self._session_pool.session_durations.append(duration)

    def record_connection_error(self) -> None:
        """Record a connection error."""
        if self._config.collect_session_pool:
            self._session_pool.connection_errors += 1

    def update_avg_session_duration(self) -> None:
        """Update average session duration."""
        if self._config.collect_session_pool and self._session_pool.session_durations:
            self._session_pool.avg_session_duration = sum(self._session_pool.session_durations) / len(self._session_pool.session_durations)

    # Rate Limiting Metrics

    def record_rate_limit_hit(self, user_id: int) -> None:
        """Record a rate limit hit."""
        if self._config.collect_rate_limiting:
            self._rate_limiting.users_limited += 1
            self._rate_limiting.limit_hits_last_hour[user_id] += 1

            # Track most limited user
            if (self._rate_limiting.most_limited_user is None or
                self._rate_limiting.limit_hits_last_hour[user_id] >
                self._rate_limiting.limit_hits_last_hour.get(self._rate_limiting.most_limited_user, 0)):
                self._rate_limiting.most_limited_user = user_id

    def record_cooldown_triggered(self) -> None:
        """Record a cooldown trigger."""
        if self._config.collect_rate_limiting:
            self._rate_limiting.cooldowns_triggered += 1

    # Abuse Protection Metrics

    def record_ban_applied(self, ban_type: str) -> None:
        """Record a ban applied."""
        if self._config.collect_abuse_protection:
            self._abuse.bans_applied += 1

            if ban_type == "temporary":
                self._abuse.temporary_bans += 1
            elif ban_type == "permanent":
                self._abuse.permanent_bans += 1

    def record_warning_issued(self) -> None:
        """Record a warning issued."""
        if self._config.collect_abuse_protection:
            self._abuse.warnings_issued += 1

    def record_suspicious_user(self) -> None:
        """Record a suspicious user detected."""
        if self._config.collect_abuse_protection:
            self._abuse.suspicious_users += 1

    def record_unban_operation(self) -> None:
        """Record an unban operation."""
        if self._config.collect_abuse_protection:
            self._abuse.unban_operations += 1

    # Latency Metrics

    def record_latency(self, latency_ms: float) -> None:
        """Record end-to-end latency."""
        if self._config.collect_latency:
            self._latency.request_count += 1
            self._latency.total_latency += latency_ms
            self._latency.latencies.append(latency_ms)

            # Update stats
            if self._latency.request_count > 0:
                self._latency.avg_latency = self._latency.total_latency / self._latency.request_count

            # Calculate percentiles
            sorted_latencies = sorted(self._latency.latencies)
            n = len(sorted_latencies)
            if n > 0:
                self._latency.p50_latency = sorted_latencies[int(n * 0.5)]
                self._latency.p95_latency = sorted_latencies[int(n * 0.95)]
                self._latency.p99_latency = sorted_latencies[int(n * 0.99)]

    # Statistics and Reporting

    async def get_summary(self) -> dict:
        """
        Get telemetry summary for display.

        Returns:
            Dictionary with all metrics
        """
        return {
            "uptime_seconds": time.time() - self._start_time,
            "telegram_delivery": {
                "messages_sent": self._telegram.messages_sent,
                "messages_failed": self._telegram.messages_failed,
                "error_rate": (
                    self._telegram.messages_failed / max(1, self._telegram.messages_sent + self._telegram.messages_failed)
                ),
                "errors_last_hour": len(self._telegram.errors_last_hour),
                "last_error": self._telegram.last_error,
            } if self._config.collect_telegram_delivery else None,
            "ui_interactions": {
                "permission_approvals": self._ui.permission_approvals,
                "permission_denials": self._ui.permission_denials,
                "total_interactions": self._ui.permission_approvals + self._ui.permission_denials,
                "approval_rate": (
                    self._ui.permission_approvals / max(1, self._ui.permission_approvals + self._ui.permission_denials)
                ),
                "top_commands": dict(sorted(
                    self._ui.command_usage.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:5]),
                "button_clicks": self._ui.button_clicks,
                "message_edits": self._ui.message_edits,
                "collapsible_expands": self._ui.collapsible_expands,
            } if self._config.collect_ui_interactions else None,
            "session_pool": {
                "active_connections": self._session_pool.active_connections,
                "max_connections": self._session_pool.max_connections,
                "utilization": self._session_pool.active_connections / max(1, self._session_pool.max_connections),
                "connection_errors": self._session_pool.connection_errors,
                "disconnections": self._session_pool.disconnections,
                "avg_session_duration": self._session_pool.avg_session_duration,
            } if self._config.collect_session_pool else None,
            "rate_limiting": {
                "users_limited": self._rate_limiting.users_limited,
                "cooldowns_triggered": self._rate_limiting.cooldowns_triggered,
                "most_limited_user": self._rate_limiting.most_limited_user,
                "active_limited_users": len(self._rate_limiting.limit_hits_last_hour),
            } if self._config.collect_rate_limiting else None,
            "abuse_protection": {
                "bans_applied": self._abuse.bans_applied,
                "temporary_bans": self._abuse.temporary_bans,
                "permanent_bans": self._abuse.permanent_bans,
                "warnings_issued": self._abuse.warnings_issued,
                "suspicious_users": self._abuse.suspicious_users,
                "unban_operations": self._abuse.unban_operations,
            } if self._config.collect_abuse_protection else None,
            "latency": {
                "request_count": self._latency.request_count,
                "avg_latency_ms": round(self._latency.avg_latency, 2),
                "p50_latency_ms": round(self._latency.p50_latency, 2),
                "p95_latency_ms": round(self._latency.p95_latency, 2),
                "p99_latency_ms": round(self._latency.p99_latency, 2),
            } if self._config.collect_latency and self._latency.request_count > 0 else None,
        }

    async def cleanup_old_data(self, max_age_hours: int = 24) -> None:
        """
        Clean up old telemetry data.

        Args:
            max_age_hours: Remove data older than this
        """
        async with self._lock:
            now = time.time()

            # Clean old error records
            if self._config.collect_telegram_delivery:
                self._telegram.errors_last_hour = [
                    e for e in self._telegram.errors_last_hour
                    if now - (self._start_time + e["time"]) < max_age_hours * 3600
                ]

            # Clean old rate limit hits
            if self._config.collect_rate_limiting:
                cutoff = now - max_age_hours * 3600
                self._rate_limiting.limit_hits_last_hour = defaultdict(int, {
                    uid: count for uid, count in self._rate_limiting.limit_hits_last_hour.items()
                    if count > 0  # Keep only if data is recent
                })

            logger.info(f"Telemetry cleanup completed")

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        self._telegram = TelegramDeliveryMetrics()
        self._ui = UIInteractionMetrics()
        self._session_pool = SessionPoolMetrics()
        self._rate_limiting = RateLimitingMetrics()
        self._abuse = AbuseProtectionMetrics()
        self._latency = LatencyMetrics()
        logger.info("Telemetry metrics reset")
