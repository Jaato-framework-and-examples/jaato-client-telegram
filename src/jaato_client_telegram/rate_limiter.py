"""
Rate limiting for jaato-client-telegram.

Implements a token bucket algorithm to limit message rates per user.
Provides protection against abuse and resource exhaustion.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from jaato_client_telegram.config import RateLimitingConfig


logger = logging.getLogger(__name__)


@dataclass
class UserRateLimitState:
    """Per-user rate limit state using token bucket algorithm."""

    # Token count (refills over time)
    # Default to 0 - will be filled on first check
    minute_tokens: float = 0.0
    hour_tokens: float = 0.0

    # Last update timestamp
    last_updated: float = field(default_factory=time.time)

    # Track if this is the first check
    _initialized: bool = False

    # Count of messages in current cooldown period
    cooldown_count: int = 0

    # When cooldown ends (timestamp)
    cooldown_end: Optional[float] = None

    # Total messages tracked (for statistics)
    total_messages: int = 0


class RateLimiter:
    """
    Token bucket rate limiter for user messages.

    Each user has a bucket of tokens that refill over time:
    - Minute bucket: Refills every second
    - Hour bucket: Refills every minute
    - Cooldown: Temporary blocking after hitting limits

    Admin users can be configured to bypass all limits.
    """

    def __init__(self, config: RateLimitingConfig) -> None:
        """
        Initialize rate limiter.

        Args:
            config: Rate limiting configuration
        """
        self._config = config
        self._states: dict[int, UserRateLimitState] = defaultdict(UserRateLimitState)
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self,
        user_id: int,
        admin_user_ids: list[int],
    ) -> tuple[bool, str, dict[str, int]]:
        """
        Check if user is allowed to send a message.

        Args:
            user_id: Telegram user ID
            admin_user_ids: List of admin user IDs (bypass limits)

        Returns:
            Tuple of (allowed: bool, message: str, stats: dict)

            - allowed: True if message allowed, False if rate limited
            - message: User-facing message (empty if allowed)
            - stats: Rate limit statistics for display
        """
        # Admin bypass
        if admin_user_ids and user_id in admin_user_ids:
            logger.debug(f"User {user_id} is admin, bypassing rate limit")
            return True, "", self._get_stats(user_id, bypass=True)

        async with self._lock:
            state = self._states[user_id]
            now = time.time()

            # Initialize tokens on first check
            if not state._initialized:
                state.minute_tokens = float(self._config.messages_per_minute)
                state.hour_tokens = float(self._config.messages_per_hour)
                state.last_updated = now
                state._initialized = True
                logger.debug(f"Initialized rate limit state for user {user_id}")

            # Refill tokens based on elapsed time
            self._refill_tokens(state, now)

            # Check if in cooldown
            if state.cooldown_end and now < state.cooldown_end:
                remaining = int(state.cooldown_end - now)
                return (
                    False,
                    f"⏸️ Rate limited. Please wait {remaining}s before sending more messages.",
                    self._get_stats(user_id, cooldown_remaining=remaining),
                )

            # Check minute limit
            if state.minute_tokens < 1.0:
                logger.warning(f"User {user_id} hit minute rate limit")
                self._apply_cooldown(state, now, "minute")
                remaining = int(state.cooldown_end - now)
                return (
                    False,
                    f"⚠️ Rate limit exceeded ({self._config.messages_per_minute}/min). "
                    f"Please wait {remaining}s.",
                    self._get_stats(user_id, cooldown_remaining=remaining),
                )

            # Check hour limit
            if state.hour_tokens < 1.0:
                logger.warning(f"User {user_id} hit hour rate limit")
                self._apply_cooldown(state, now, "hour")
                remaining = int(state.cooldown_end - now)
                return (
                    False,
                    f"⚠️ Rate limit exceeded ({self._config.messages_per_hour}/hour). "
                    f"Please wait {remaining}s.",
                    self._get_stats(user_id, cooldown_remaining=remaining),
                )

            # Allow message - consume tokens
            state.minute_tokens -= 1.0
            state.hour_tokens -= 1.0
            state.last_updated = now
            state.total_messages += 1
            state.cooldown_count = 0  # Reset cooldown count on success

            return True, "", self._get_stats(user_id)

    def _refill_tokens(self, state: UserRateLimitState, now: float) -> None:
        """Refill tokens based on elapsed time since last update."""
        elapsed = now - state.last_updated

        if elapsed <= 0:
            return

        # Refill minute bucket: refills every second
        # tokens_per_second = messages_per_minute / 60
        minute_refill = (self._config.messages_per_minute / 60.0) * elapsed
        state.minute_tokens = min(
            self._config.messages_per_minute,
            state.minute_tokens + minute_refill,
        )

        # Refill hour bucket: refills every minute
        # tokens_per_minute = messages_per_hour / 60
        hour_refill = (self._config.messages_per_hour / 60.0) * elapsed
        state.hour_tokens = min(
            self._config.messages_per_hour,
            state.hour_tokens + hour_refill,
        )

        state.last_updated = now

    def _apply_cooldown(
        self, state: UserRateLimitState, now: float, limit_type: str
    ) -> None:
        """Apply cooldown penalty after hitting rate limit."""
        state.cooldown_count += 1

        # Longer cooldown for repeated violations
        multiplier = min(3.0, 1.0 + (state.cooldown_count * 0.5))
        cooldown = int(self._config.cooldown_seconds * multiplier)

        state.cooldown_end = now + cooldown

        logger.info(
            f"Applied {cooldown}s cooldown for user "
            f"(violation #{state.cooldown_count}, type={limit_type})"
        )

    def _get_stats(
        self,
        user_id: int,
        bypass: bool = False,
        cooldown_remaining: int = 0,
    ) -> dict[str, int]:
        """Get rate limit statistics for display."""
        state = self._states[user_id]

        stats = {
            "minute_available": int(state.minute_tokens),
            "minute_limit": self._config.messages_per_minute,
            "hour_available": int(state.hour_tokens),
            "hour_limit": self._config.messages_per_hour,
            "total_messages": state.total_messages,
            "is_bypassed": bypass,
        }

        if cooldown_remaining > 0:
            stats["cooldown_remaining"] = cooldown_remaining

        return stats

    async def get_user_stats(self, user_id: int) -> dict[str, int]:
        """
        Get current rate limit statistics for a user.

        Args:
            user_id: Telegram user ID

        Returns:
            Dictionary with rate limit statistics
        """
        async with self._lock:
            state = self._states[user_id]
            now = time.time()
            self._refill_tokens(state, now)
            return self._get_stats(user_id)

    async def get_all_stats(self) -> dict[int, dict[str, int]]:
        """
        Get statistics for all tracked users.

        Returns:
            Dictionary mapping user_id to their stats
        """
        async with self._lock:
            now = time.time()
            result = {}
            for user_id, state in self._states.items():
                self._refill_tokens(state, now)
                result[user_id] = self._get_stats(user_id)
            return result

    async def reset_user(self, user_id: int) -> None:
        """
        Reset rate limit state for a user (admin function).

        Args:
            user_id: Telegram user ID to reset
        """
        async with self._lock:
            if user_id in self._states:
                del self._states[user_id]
                logger.info(f"Reset rate limit state for user {user_id}")

    async def cleanup_old_states(self, max_age_hours: int = 24) -> int:
        """
        Clean up old user states to prevent memory bloat.

        Args:
            max_age_hours: Remove states older than this

        Returns:
            Number of states removed
        """
        async with self._lock:
            now = time.time()
            to_remove = []

            for user_id, state in self._states.items():
                age_hours = (now - state.last_updated) / 3600.0
                if age_hours > max_age_hours:
                    to_remove.append(user_id)

            for user_id in to_remove:
                del self._states[user_id]

            if to_remove:
                logger.info(f"Cleaned up {len(to_remove)} old rate limit states")

            return len(to_remove)

    async def start_cleanup_task(self, interval_minutes: int = 60) -> asyncio.Task[None]:
        """
        Start background task to clean up old states.

        Args:
            interval_minutes: How often to run cleanup

        Returns:
            The running cleanup task
        """
        async def cleanup_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(interval_minutes * 60)
                    await self.cleanup_old_states()
                except asyncio.CancelledError:
                    logger.info("Rate limiter cleanup task cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error in rate limiter cleanup: {e}")

        task = asyncio.create_task(cleanup_loop(), name="rate_limiter_cleanup")
        logger.info(f"Started rate limiter cleanup task (interval={interval_minutes}min)")
        return task
