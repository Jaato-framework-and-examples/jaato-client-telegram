"""
Abuse protection system for jaato-client-telegram.

Detects and mitigates abusive behavior patterns:
- Suspicious activity detection (spam, rapid messages, etc.)
- User reputation tracking
- Temporary and permanent bans
- Abuse incident logging
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from jaato_client_telegram.config import AbuseProtectionConfig


logger = logging.getLogger(__name__)


class BanLevel(Enum):
    """Severity level of bans."""
    WARNING = "warning"      # Warning message, no restriction
    TEMPORARY = "temporary"  # Temporary ban
    PERMANENT = "permanent"  # Permanent ban


@dataclass
class AbuseIncident:
    """Record of a detected abuse incident."""
    timestamp: float
    user_id: int
    incident_type: str
    severity: str
    details: str
    score: float


@dataclass
class UserAbuseState:
    """Per-user abuse state and tracking."""

    # Message timing for detecting rapid messages (last 100 messages)
    message_timestamps: deque = field(default_factory=lambda: deque(maxlen=100))

    # Abuse incidents history
    incidents: list[AbuseIncident] = field(default_factory=list)

    # Current ban state
    is_banned: bool = False
    ban_level: Optional[BanLevel] = None
    ban_reason: Optional[str] = None
    ban_end_time: Optional[float] = None  # For temporary bans

    # Reputation score (starts at 100, decreases with abuse, increases with good behavior)
    reputation: float = 100.0

    # Total messages tracked
    total_messages: int = 0

    # Suspicion score (0-100, higher = more suspicious)
    suspicion_score: float = 0.0

    # Last updated
    last_updated: float = field(default_factory=time.time)


class AbuseProtector:
    """
    Abuse detection and protection system.

    Monitors user behavior for suspicious patterns and applies graduated sanctions:
    1. Warning (reputation hit)
    2. Temporary ban (configurable duration)
    3. Permanent ban (admin only)

    Features:
    - Rapid message detection
    - Repetitive content detection
    - Reputation-based leniency
    - Automatic escalation
    """

    def __init__(self, config: AbuseProtectionConfig) -> None:
        """
        Initialize abuse protector.

        Args:
            config: Abuse protection configuration
        """
        self._config = config
        self._states: dict[int, UserAbuseState] = {}
        self._lock = asyncio.Lock()

        logger.info(
            f"AbuseProtector initialized: "
            f"max_rapid={config.max_rapid_messages}, "
            f"rapid_interval={config.rapid_message_interval}s, "
            f"reputation_threshold={config.reputation_threshold}"
        )

    async def check_message(
        self,
        user_id: int,
        message_text: str,
        admin_user_ids: list[int],
    ) -> tuple[bool, str, dict]:
        """
        Check if a message should be allowed based on abuse detection.

        Args:
            user_id: Telegram user ID
            message_text: Message content (for repetitive content detection)
            admin_user_ids: List of admin user IDs (immune to abuse checks)

        Returns:
            Tuple of (allowed: bool, message: str, context: dict)

            - allowed: True if message allowed, False if blocked
            - message: User-facing message (empty if allowed)
            - context: Abuse detection context for logging
        """
        # Admins bypass all abuse checks
        if admin_user_ids and user_id in admin_user_ids:
            return True, "", {"bypass": True, "reason": "admin"}

        async with self._lock:
            state = self._states.get(user_id)

            if state is None:
                state = UserAbuseState()
                self._states[user_id] = state

            # Check if user is banned
            if state.is_banned:
                return self._check_ban_status(state)

            # Update message tracking
            now = time.time()
            state.message_timestamps.append(now)
            state.total_messages += 1
            state.last_updated = now

            # Run abuse detection checks
            detections = []

            # 1. Rapid message detection
            rapid_count = self._detect_rapid_messages(state, now)
            if rapid_count > 0:
                detections.append({
                    "type": "rapid_messages",
                    "severity": "high",
                    "score": rapid_count * 10,
                    "details": f"{rapid_count} messages in {self._config.rapid_message_interval}s"
                })

            # 2. Suspicion score calculation
            state.suspicion_score = self._calculate_suspicion_score(state, detections)

            # 3. Escalation check
            if state.suspicion_score >= self._config.suspicion_threshold:
                return self._escalate_abuse(state, detections, user_id)

            # Good behavior: slowly increase reputation
            if state.suspicion_score < 20:
                state.reputation = min(100.0, state.reputation + 0.1)

            context = {
                "suspicion_score": state.suspicion_score,
                "reputation": state.reputation,
                "detections": detections,
            }

            return True, "", context

    def _detect_rapid_messages(self, state: UserAbuseState, now: float) -> int:
        """
        Detect rapid message sending.

        Args:
            state: User abuse state
            now: Current timestamp

        Returns:
            Number of messages in rapid interval
        """
        # Count messages in the last rapid_message_interval seconds
        cutoff = now - self._config.rapid_message_interval
        recent_count = sum(1 for ts in state.message_timestamps if ts > cutoff)

        return recent_count

    def _calculate_suspicion_score(
        self, state: UserAbuseState, detections: list[dict]
    ) -> float:
        """
        Calculate overall suspicion score based on detections.

        Args:
            state: User abuse state
            detections: List of detected abuse patterns

        Returns:
            Suspicion score (0-100)
        """
        base_score = 0.0

        for detection in detections:
            base_score += detection["score"]

        # Adjust based on reputation (lower reputation = higher suspicion)
        reputation_factor = 1.0 - (state.reputation / 200.0)  # 0.5 to 1.0
        adjusted_score = base_score * reputation_factor

        # Decay suspicion over time (if no recent abuse)
        time_since_update = time.time() - state.last_updated
        decay = min(0.5, time_since_update / 3600.0)  # Max 50% decay per hour
        final_score = adjusted_score * (1.0 - decay)

        return min(100.0, max(0.0, final_score))

    def _check_ban_status(self, state: UserAbuseState) -> tuple[bool, str, dict]:
        """
        Check if a ban is still active.

        Args:
            state: User abuse state

        Returns:
            Tuple of (allowed: bool, message: str, context: dict)
        """
        if not state.is_banned:
            return True, "", {"banned": False}

        now = time.time()

        # Check if temporary ban has expired
        if state.ban_level == BanLevel.TEMPORARY and state.ban_end_time:
            if now >= state.ban_end_time:
                # Ban expired, unban user
                state.is_banned = False
                state.ban_level = None
                state.ban_reason = None
                state.ban_end_time = None
                logger.info(f"Temporary ban expired for user {state.message_timestamps[0] if state.message_timestamps else 'unknown'}")
                return True, "", {"banned": False, "ban_expired": True}

        # User is still banned
        if state.ban_level == BanLevel.PERMANENT:
            return (
                False,
                f"🚫 Your account has been permanently banned.\n\nReason: {state.ban_reason}",
                {"banned": True, "ban_level": "permanent", "reason": state.ban_reason}
            )
        else:  # TEMPORARY
            remaining = int(state.ban_end_time - now) if state.ban_end_time else 0
            return (
                False,
                f"⏸️ Your account is temporarily banned.\n\n"
                f"Reason: {state.ban_reason}\n"
                f"Time remaining: {remaining}s",
                {"banned": True, "ban_level": "temporary", "remaining": remaining, "reason": state.ban_reason}
            )

    def _escalate_abuse(
        self,
        state: UserAbuseState,
        detections: list[dict],
        user_id: int,
    ) -> tuple[bool, str, dict]:
        """
        Escalate abuse based on severity.

        Args:
            state: User abuse state
            detections: List of detected abuse patterns
            user_id: Telegram user ID

        Returns:
            Tuple of (allowed: bool, message: str, context: dict)
        """
        state.reputation -= 10  # Reputation hit

        # Determine escalation level based on reputation and suspicion
        if state.reputation < self._config.reputation_threshold:
            # Low reputation: apply ban
            if state.suspicion_score >= 80:
                return self._apply_ban(state, user_id, BanLevel.PERMANENT, "Severe abuse detected")
            else:
                return self._apply_ban(state, user_id, BanLevel.TEMPORARY, "Abuse pattern detected")
        else:
            # Still has reputation: warning
            logger.warning(f"Abuse warning for user {user_id}: {detections}")
            state.incidents.append(AbuseIncident(
                timestamp=time.time(),
                user_id=user_id,
                incident_type="warning",
                severity="medium",
                details=str(detections),
                score=state.suspicion_score,
            ))
            return (
                True,
                f"⚠️ Warning: Suspicious activity detected. "
                f"Please slow down to avoid restrictions.",
                {"warning": True, "suspicion_score": state.suspicion_score}
            )

    def _apply_ban(
        self,
        state: UserAbuseState,
        user_id: int,
        ban_level: BanLevel,
        reason: str,
    ) -> tuple[bool, str, dict]:
        """
        Apply a ban to a user.

        Args:
            state: User abuse state
            user_id: Telegram user ID
            ban_level: Ban level to apply
            reason: Reason for the ban

        Returns:
            Tuple of (allowed: bool, message: str, context: dict)
        """
        state.is_banned = True
        state.ban_level = ban_level
        state.ban_reason = reason

        if ban_level == BanLevel.TEMPORARY:
            duration = self._config.temporary_ban_duration
            state.ban_end_time = time.time() + duration
            logger.warning(f"Temporary ban applied to user {user_id}: {duration}s - {reason}")

            # Record incident
            state.incidents.append(AbuseIncident(
                timestamp=time.time(),
                user_id=user_id,
                incident_type="temporary_ban",
                severity="high",
                details=reason,
                score=state.suspicion_score,
            ))

            return (
                False,
                f"⏸️ You have been temporarily banned for {duration} seconds.\n\n"
                f"Reason: {reason}",
                {"banned": True, "ban_level": "temporary", "duration": duration, "reason": reason}
            )
        else:  # PERMANENT
            state.ban_end_time = None
            logger.error(f"Permanent ban applied to user {user_id}: {reason}")

            # Record incident
            state.incidents.append(AbuseIncident(
                timestamp=time.time(),
                user_id=user_id,
                incident_type="permanent_ban",
                severity="critical",
                details=reason,
                score=state.suspicion_score,
            ))

            return (
                False,
                f"🚫 You have been permanently banned.\n\nReason: {reason}",
                {"banned": True, "ban_level": "permanent", "reason": reason}
            )

    async def ban_user(
        self,
        user_id: int,
        ban_level: BanLevel = BanLevel.TEMPORARY,
        reason: str = "Manual ban by admin",
        duration: Optional[int] = None,
    ) -> None:
        """
        Manually ban a user (admin function).

        Args:
            user_id: Telegram user ID to ban
            ban_level: Ban level (temporary or permanent)
            reason: Reason for the ban
            duration: Duration for temporary ban (uses config default if None)
        """
        async with self._lock:
            state = self._states.get(user_id)

            if state is None:
                state = UserAbuseState()
                self._states[user_id] = state

            state.is_banned = True
            state.ban_level = ban_level
            state.ban_reason = reason

            if ban_level == BanLevel.TEMPORARY:
                ban_duration = duration or self._config.temporary_ban_duration
                state.ban_end_time = time.time() + ban_duration
                logger.info(f"Admin: Temporary ban applied to user {user_id}: {ban_duration}s - {reason}")
            else:  # PERMANENT
                state.ban_end_time = None
                logger.warning(f"Admin: Permanent ban applied to user {user_id}: {reason}")

    async def unban_user(self, user_id: int) -> None:
        """
        Unban a user (admin function).

        Args:
            user_id: Telegram user ID to unban
        """
        async with self._lock:
            state = self._states.get(user_id)

            if state and state.is_banned:
                state.is_banned = False
                state.ban_level = None
                state.ban_reason = None
                state.ban_end_time = None
                logger.info(f"Admin: User {user_id} unbanned")

    async def get_user_stats(self, user_id: int) -> Optional[dict]:
        """
        Get abuse protection statistics for a user.

        Args:
            user_id: Telegram user ID

        Returns:
            Dictionary with user stats, or None if user not tracked
        """
        async with self._lock:
            state = self._states.get(user_id)

            if state is None:
                return None

            return {
                "banned": state.is_banned,
                "ban_level": state.ban_level.value if state.ban_level else None,
                "ban_reason": state.ban_reason,
                "ban_end_time": state.ban_end_time,
                "reputation": state.reputation,
                "suspicion_score": state.suspicion_score,
                "total_messages": state.total_messages,
                "incidents": len(state.incidents),
            }

    async def get_all_stats(self) -> dict[int, dict]:
        """
        Get statistics for all tracked users.

        Returns:
            Dictionary mapping user_id to their stats
        """
        async with self._lock:
            result = {}
            for user_id, state in self._states.items():
                result[user_id] = {
                    "banned": state.is_banned,
                    "ban_level": state.ban_level.value if state.ban_level else None,
                    "reputation": state.reputation,
                    "suspicion_score": state.suspicion_score,
                    "total_messages": state.total_messages,
                    "incidents": len(state.incidents),
                }
            return result

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
                if age_hours > max_age_hours and not state.is_banned:
                    to_remove.append(user_id)

            for user_id in to_remove:
                del self._states[user_id]

            if to_remove:
                logger.info(f"Cleaned up {len(to_remove)} old abuse protection states")

            return len(to_remove)
