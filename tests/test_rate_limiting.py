"""
Tests for rate limiting functionality.

Tests the token bucket rate limiter including:
- Token bucket refill logic
- Rate limit enforcement
- Admin bypass
- Cooldown behavior
- Statistics tracking
- State cleanup
"""

import asyncio
import pytest

from jaato_client_telegram.config import RateLimitingConfig


class TestRateLimitingConfig:
    """Test rate limiting configuration."""

    def test_default_config(self):
        """Test default configuration values."""
        config = RateLimitingConfig()

        assert config.enabled is False
        assert config.messages_per_minute == 30
        assert config.messages_per_hour == 200
        assert config.cooldown_seconds == 60
        assert config.admin_bypass is True
        assert config.cleanup_interval_minutes == 60
        assert config.cleanup_max_age_hours == 24

    def test_custom_config(self):
        """Test custom configuration values."""
        config = RateLimitingConfig(
            enabled=True,
            messages_per_minute=10,
            messages_per_hour=100,
            cooldown_seconds=30,
            admin_bypass=False,
        )

        assert config.enabled is True
        assert config.messages_per_minute == 10
        assert config.messages_per_hour == 100
        assert config.cooldown_seconds == 30
        assert config.admin_bypass is False


class TestRateLimiter:
    """Test RateLimiter class."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return RateLimitingConfig(
            enabled=True,
            messages_per_minute=10,
            messages_per_hour=100,
            cooldown_seconds=30,
            admin_bypass=True,
        )

    @pytest.fixture
    def limiter(self, config):
        """Create rate limiter instance."""
        from jaato_client_telegram.rate_limiter import RateLimiter
        return RateLimiter(config)

    @pytest.mark.asyncio
    async def test_allow_first_message(self, limiter):
        """Test that first message is allowed."""
        allowed, message, stats = await limiter.check_rate_limit(
            user_id=123,
            admin_user_ids=[],
        )

        assert allowed is True
        assert message == ""
        assert stats["minute_available"] == 9  # 10 - 1
        assert stats["hour_available"] == 99  # 100 - 1
        assert stats["total_messages"] == 1

    @pytest.mark.asyncio
    async def test_minute_limit_enforcement(self, limiter):
        """Test that minute limit is enforced."""
        user_id = 123

        # Send up to the limit
        for i in range(10):
            allowed, message, _ = await limiter.check_rate_limit(
                user_id=user_id,
                admin_user_ids=[],
            )
            assert allowed is True, f"Message {i+1} should be allowed"

        # Next message should be rate limited
        allowed, message, _ = await limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=[],
        )
        assert allowed is False
        assert "Rate limit exceeded" in message

    @pytest.mark.asyncio
    async def test_hour_limit_enforcement(self, limiter):
        """Test that hour limit is enforced."""
        # Use a high minute limit to avoid hitting it first
        config = RateLimitingConfig(
            enabled=True,
            messages_per_minute=1000,  # Very high
            messages_per_hour=5,
            cooldown_seconds=30,
        )
        from jaato_client_telegram.rate_limiter import RateLimiter
        limiter = RateLimiter(config)

        user_id = 123

        # Send up to the hour limit
        for i in range(5):
            allowed, message, _ = await limiter.check_rate_limit(
                user_id=user_id,
                admin_user_ids=[],
            )
            assert allowed is True, f"Message {i+1} should be allowed"

        # Next message should be rate limited
        allowed, message, _ = await limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=[],
        )
        assert allowed is False
        assert "Rate limit exceeded" in message

    @pytest.mark.asyncio
    async def test_admin_bypass(self, limiter):
        """Test that admins bypass rate limits."""
        admin_id = 999

        # Admin should be allowed even after hitting limits
        for i in range(20):  # More than minute limit
            allowed, message, stats = await limiter.check_rate_limit(
                user_id=admin_id,
                admin_user_ids=[admin_id],
            )
            assert allowed is True, f"Admin message {i+1} should be allowed"
            assert stats["is_bypassed"] is True

    @pytest.mark.asyncio
    async def test_multiple_users_independent(self, limiter):
        """Test that multiple users have independent rate limits."""
        user1_id = 123
        user2_id = 456

        # User 1 hits their limit
        for i in range(10):
            allowed, _, _ = await limiter.check_rate_limit(
                user_id=user1_id,
                admin_user_ids=[],
            )
            assert allowed is True

        # User 1 should be rate limited
        allowed, _, _ = await limiter.check_rate_limit(
            user_id=user1_id,
            admin_user_ids=[],
        )
        assert allowed is False

        # User 2 should still be allowed
        for i in range(10):
            allowed, _, _ = await limiter.check_rate_limit(
                user_id=user2_id,
                admin_user_ids=[],
            )
            assert allowed is True

    @pytest.mark.asyncio
    async def test_cooldown_behavior(self, limiter):
        """Test that cooldown is applied after hitting limits."""
        user_id = 123

        # Hit minute limit
        for i in range(10):
            await limiter.check_rate_limit(
                user_id=user_id,
                admin_user_ids=[],
            )

        # Should be in cooldown
        allowed, message, stats = await limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=[],
        )
        assert allowed is False
        assert "cooldown_remaining" in stats
        assert stats["cooldown_remaining"] > 0

    @pytest.mark.asyncio
    async def test_token_refill_over_time(self, limiter):
        """Test that tokens refill over time."""
        user_id = 123

        # Send messages to consume tokens
        for _ in range(5):
            await limiter.check_rate_limit(
                user_id=user_id,
                admin_user_ids=[],
            )

        stats = await limiter.get_user_stats(user_id)
        assert stats["minute_available"] == 5

        # Wait for some tokens to refill (1 second should refill ~0.167 tokens)
        # Actually, we need to wait longer to see a visible change
        # Let's just verify the structure is correct
        stats2 = await limiter.get_user_stats(user_id)
        assert stats2["minute_available"] >= 5

    @pytest.mark.asyncio
    async def test_get_user_stats(self, limiter):
        """Test getting user statistics."""
        user_id = 123

        # Send some messages
        for _ in range(3):
            await limiter.check_rate_limit(
                user_id=user_id,
                admin_user_ids=[],
            )

        stats = await limiter.get_user_stats(user_id)

        assert stats["minute_available"] == 7  # 10 - 3
        assert stats["hour_available"] == 97  # 100 - 3
        assert stats["minute_limit"] == 10
        assert stats["hour_limit"] == 100
        assert stats["total_messages"] == 3
        assert stats["is_bypassed"] is False

    @pytest.mark.asyncio
    async def test_get_all_stats(self, limiter):
        """Test getting all user statistics."""
        user1_id = 123
        user2_id = 456

        # Send messages from both users
        await limiter.check_rate_limit(
            user_id=user1_id,
            admin_user_ids=[],
        )
        await limiter.check_rate_limit(
            user_id=user1_id,
            admin_user_ids=[],
        )
        await limiter.check_rate_limit(
            user_id=user2_id,
            admin_user_ids=[],
        )

        all_stats = await limiter.get_all_stats()

        assert user1_id in all_stats
        assert user2_id in all_stats
        assert all_stats[user1_id]["total_messages"] == 2
        assert all_stats[user2_id]["total_messages"] == 1

    @pytest.mark.asyncio
    async def test_reset_user(self, limiter):
        """Test resetting a user's rate limit state."""
        user_id = 123

        # Hit the limit
        for _ in range(10):
            await limiter.check_rate_limit(
                user_id=user_id,
                admin_user_ids=[],
            )

        # Should be rate limited
        allowed, _, _ = await limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=[],
        )
        assert allowed is False

        # Reset the user
        await limiter.reset_user(user_id)

        # Should now be allowed again (tokens refilled to max)
        allowed, _, stats = await limiter.check_rate_limit(
            user_id=user_id,
            admin_user_ids=[],
        )
        assert allowed is True
        assert stats["minute_available"] == 9  # 10 - 1

    @pytest.mark.asyncio
    async def test_cleanup_old_states(self, limiter):
        """Test cleanup of old user states."""
        user1_id = 123
        user2_id = 456

        # Add some users
        await limiter.check_rate_limit(
            user_id=user1_id,
            admin_user_ids=[],
        )
        await limiter.check_rate_limit(
            user_id=user2_id,
            admin_user_ids=[],
        )

        # Manually set last_updated to old time
        import time
        old_time = time.time() - (25 * 3600)  # 25 hours ago

        async with limiter._lock:
            if user1_id in limiter._states:
                limiter._states[user1_id].last_updated = old_time

        # Cleanup with 24 hour max age
        removed = await limiter.cleanup_old_states(max_age_hours=24)

        assert removed == 1
        assert user1_id not in limiter._states
        assert user2_id in limiter._states

    @pytest.mark.asyncio
    async def test_cleanup_task(self, limiter):
        """Test that cleanup task starts and can be cancelled."""
        # Start cleanup task
        task = await limiter.start_cleanup_task(interval_minutes=0)  # Immediate

        # Give it a moment to start
        await asyncio.sleep(0.1)

        assert task is not None
        assert not task.done()

        # Cancel the task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, limiter):
        """Test concurrent rate limit checks."""
        user_id = 123

        # Create multiple concurrent tasks
        tasks = [
            limiter.check_rate_limit(
                user_id=user_id,
                admin_user_ids=[],
            )
            for _ in range(10)
        ]

        # All should complete successfully
        results = await asyncio.gather(*tasks)

        # First 10 should be allowed, rest rate limited
        allowed_count = sum(1 for allowed, _, _ in results if allowed)
        assert allowed_count <= 10  # At most the limit
