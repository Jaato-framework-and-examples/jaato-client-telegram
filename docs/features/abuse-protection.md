# Abuse Protection System - Implementation Guide

## Overview

The jaato-client-telegram bot includes a comprehensive abuse protection system that detects and mitigates abusive behavior patterns. The system provides layered defense when combined with rate limiting.

## Architecture

### Components

1. **AbuseProtector** (`abuse_protection.py`)
   - Core abuse detection and mitigation logic
   - Tracks per-user state and reputation
   - Manages ban escalation
   - Provides statistics and admin interface

2. **UserAbuseState** (dataclass)
   - Per-user tracking data
   - Message timestamps for rapid detection
   - Reputation and suspicion scores
   - Ban status and incidents

3. **AbuseIncident** (dataclass)
   - Record of detected abuse
   - Timestamp, type, severity, score
   - Used for audit trail

4. **BanLevel** (enum)
   - `WARNING` - Warning message only
   - `TEMPORARY` - Temporary time-based ban
   - `PERMANENT` - Permanent ban (admin removal required)

## Detection Algorithms

### 1. Rapid Message Detection

**Algorithm:**
```python
# Track last 100 message timestamps per user
message_timestamps = deque(maxlen=100)

# Count messages in configured interval (e.g., last 3 seconds)
cutoff = now - rapid_message_interval
recent_count = sum(1 for ts in message_timestamps if ts > cutoff)

# Calculate suspicion score
suspicion_score = recent_count * 10
```

**Configuration:**
```yaml
abuse_protection:
  max_rapid_messages: 5
  rapid_message_interval: 3  # seconds
```

**Example:**
- User sends 5 messages in 3 seconds → suspicion_score = 50
- User sends 10 messages in 3 seconds → suspicion_score = 100

### 2. Suspicion Score Calculation

**Formula:**
```python
# Base score from detections
base_score = sum(detection["score"] for detection in detections)

# Adjust by reputation (lower reputation = higher suspicion)
reputation_factor = 1.0 - (reputation / 200.0)  # 0.5 to 1.0
adjusted_score = base_score * reputation_factor

# Time decay (suspicion fades over time)
time_elapsed = now - last_updated
decay = min(0.5, time_elapsed / 3600.0)  # Max 50% decay per hour
final_score = adjusted_score * (1.0 - decay)
```

**Reputation Impact:**
- Reputation 100 → suspicion_score × 0.5 (50% reduction)
- Reputation 50 → suspicion_score × 0.75 (25% reduction)
- Reputation 0 → suspicion_score × 1.0 (no reduction)

### 3. Escalation Logic

```python
if suspicion_score >= suspicion_threshold:
    if reputation < reputation_threshold:
        # Low reputation: apply ban
        if suspicion_score >= 80:
            apply_ban(BanLevel.PERMANENT)
        else:
            apply_ban(BanLevel.TEMPORARY)
    else:
        # Still has reputation: warning
        send_warning_message()
```

**Escalation Matrix:**

| Suspicion Score | Reputation | Action |
|----------------|------------|--------|
| < 70 | Any | No action |
| ≥ 70 | ≥ threshold | Warning message |
| ≥ 70 | < threshold, < 80 | Temporary ban |
| ≥ 80 | < threshold | Permanent ban |

**Configuration:**
```yaml
abuse_protection:
  suspicion_threshold: 70      # Score to trigger escalation
  reputation_threshold: 30.0   # Below this: bans apply
```

## User Reputation System

### Scoring

**Initial State:**
- New users start with reputation = 100.0
- Suspicion score = 0.0

**Good Behavior:**
```python
# Slow reputation increase when behavior is good
if suspicion_score < 20:
    reputation = min(100.0, reputation + 0.1)
```

**Abuse:**
```python
# Reputation hit on escalation
reputation -= 10.0
```

**Reputation Levels:**

| Range | Level | Behavior |
|-------|-------|----------|
| 70-100 | High | Lenient treatment, warnings only |
| 40-69 | Medium | Standard detection thresholds |
| 20-39 | Low | Stricter detection, easier to ban |
| 0-19 | Critical | Very strict, bans easily |

### Recovery

Users can recover reputation by:
1. Waiting (time decay reduces suspicion)
2. Sending normal messages slowly (suspicion stays low)
3. Admins can manually unban users

## Ban Management

### Temporary Ban

**Duration:**
```yaml
abuse_protection:
  temporary_ban_duration: 300  # 5 minutes default
```

**Behavior:**
```python
state.is_banned = True
state.ban_level = BanLevel.TEMPORARY
state.ban_end_time = now + duration

# Check expires automatically
if now >= state.ban_end_time:
    unban_user()
```

**User Message:**
```
⏸️ Your account is temporarily banned.

Reason: Abuse pattern detected
Time remaining: 287s
```

### Permanent Ban

**Behavior:**
```python
state.is_banned = True
state.ban_level = BanLevel.PERMANENT
state.ban_end_time = None  # Never expires

# Can only be removed by admin unban
```

**User Message:**
```
🚫 Your account has been permanently banned.

Reason: TOS violation
```

### Admin Commands

**Ban User:**
```bash
# Permanent ban (default)
/ban 123456789 Spamming

# Temporary ban
/ban 123456789 --temp Abusive behavior

# Permanent ban with flag
/ban 123456789 --perm TOS violation
```

**Unban User:**
```bash
/unban 123456789
```

**View Statistics:**
```bash
/abuse_stats
```

**Output Example:**
```
📊 Abuse Protection Statistics

Tracked Users: 15

User 123456789: 234 msgs | suspicion=85.3 | rep=15.0🔴 | 🚫 temporary
User 987654321: 45 msgs | suspicion=12.1 | rep=95.0🟢
User 555666777: 89 msgs | suspicion=45.6 | rep=62.3🟡
```

## Integration with Rate Limiting

### Message Flow

```
User Message
    ↓
[1] Abuse Protection Check
    ↓ (if not blocked)
[2] Rate Limit Check
    ↓ (if not limited)
[3] Whitelist Check
    ↓ (if allowed)
[4] Process Message
```

### Configuration Example

```yaml
# jaato-client-telegram.yaml

# Abuse protection (first layer)
abuse_protection:
  enabled: true
  max_rapid_messages: 5
  rapid_message_interval: 3
  suspicion_threshold: 70
  reputation_threshold: 30.0
  temporary_ban_duration: 300
  admin_bypass: true

# Rate limiting (second layer)
rate_limiting:
  enabled: true
  messages_per_minute: 30
  messages_per_hour: 200
  cooldown_seconds: 60
  admin_bypass: true
```

### Layered Defense

| Layer | Detection | Action |
|-------|-----------|--------|
| Abuse Protection | Rapid messages, patterns | Ban/warn |
| Rate Limiting | Message frequency | Cooldown |

**Example Scenario:**

User sends 100 messages in 10 seconds:

1. **Abuse Protection** detects rapid messages
   - Suspicion score: 100 (10 messages × 10)
   - Reputation: 100 → 90 (after first escalation)
   - Action: Temporary ban (300s)

2. **Rate Limiting** would also trigger
   - Exceeds 30 messages/minute
   - Action: Cooldown (60s)

**Result:** Abuse protection catches it first with more severe consequence.

## Implementation Examples

### Example 1: Custom Detection Rule

**Scenario:** Want to detect users sending the same message repeatedly.

**Add to `AbuseProtector`:**

```python
def _detect_repetitive_messages(
    self,
    state: UserAbuseState,
    message_text: str,
) -> int:
    """Detect repetitive message content."""
    if not hasattr(state, '_last_messages'):
        state._last_messages = deque(maxlen=10)
    
    state._last_messages.append(message_text)
    
    # Count how many times this message appears in recent history
    repeat_count = sum(
        1 for msg in state._last_messages
        if msg == message_text
    )
    
    if repeat_count >= 3:
        return repeat_count * 15  # Higher score for spam
    
    return 0
```

**Integration in `check_message()`:**

```python
# Add to detection list
detections = []

# Existing rapid detection
rapid_count = self._detect_rapid_messages(state, now)
if rapid_count > 0:
    detections.append({
        "type": "rapid_messages",
        "score": rapid_count * 10,
        "details": f"{rapid_count} messages in {interval}s"
    })

# New repetitive detection
repeat_count = self._detect_repetitive_messages(state, user_text)
if repeat_count > 0:
    detections.append({
        "type": "repetitive_messages",
        "score": repeat_count,
        "details": f"Message repeated {repeat_count} times"
    })
```

### Example 2: Custom Ban Duration

**Scenario:** Longer bans for repeat offenders.

**Add to `_apply_ban()`:**

```python
def _apply_ban(
    self,
    state: UserAbuseState,
    user_id: int,
    ban_level: BanLevel,
    reason: str,
) -> tuple[bool, str, dict]:
    """Apply ban with dynamic duration."""
    
    if ban_level == BanLevel.TEMPORARY:
        # Calculate duration based on offense count
        offense_count = len([i for i in state.incidents if i.severity >= "high"])
        
        # Escalating durations: 5min → 15min → 1hr → 24hr
        base_duration = self._config.temporary_ban_duration
        multiplier = min(288, 3 ** (offense_count - 1))
        duration = base_duration * multiplier
        
        state.ban_end_time = time.time() + duration
        
        return (
            False,
            f"⏸️ Temporarily banned for {duration}s (offense #{offense_count})",
            {"banned": True, "duration": duration}
        )
```

### Example 3: Whitelist Trusted Users

**Scenario:** Never ban specific trusted users.

**Modify `check_message()`:**

```python
async def check_message(
    self,
    user_id: int,
    message_text: str,
    admin_user_ids: list[int],
    trusted_user_ids: list[int] = None,  # New parameter
) -> tuple[bool, str, dict]:
    # Check if user is trusted
    if trusted_user_ids and user_id in trusted_user_ids:
        return True, "", {"bypass": True, "reason": "trusted_user"}
    
    # Existing admin check
    if admin_user_ids and user_id in admin_user_ids:
        return True, "", {"bypass": True, "reason": "admin"}
    
    # Continue with normal checks...
```

### Example 4: Custom Escalation

**Scenario:** 3-strike system before ban.

**Add to `AbuseProtector`:**

```python
def _escalate_abuse(
    self,
    state: UserAbuseState,
    detections: list[dict],
    user_id: int,
) -> tuple[bool, str, dict]:
    """Three-strike escalation system."""
    
    # Count recent incidents
    recent_incidents = [
        i for i in state.incidents
        if time.time() - i.timestamp < 3600  # Last hour
    ]
    
    if len(recent_incidents) < 3:
        # Strike 1 or 2: Warning
        state.incidents.append(AbuseIncident(
            timestamp=time.time(),
            user_id=user_id,
            incident_type="warning",
            severity="medium",
            details=f"Strike {len(recent_incidents) + 1}",
            score=state.suspicion_score,
        ))
        
        return (
            True,
            f"⚠️ Warning: Strike {len(recent_incidents) + 1}/3. "
            f"Please slow down to avoid restrictions.",
            {"warning": True, "strike": len(recent_incidents) + 1}
        )
    else:
        # Strike 3: Ban
        return self._apply_ban(state, user_id, BanLevel.TEMPORARY, "3 strikes")
```

### Example 5: Abuse Reporting

**Scenario:** Generate daily abuse report for admins.

**Add to `AbuseProtector`:**

```python
async def generate_abuse_report(self) -> str:
    """Generate abuse report for admins."""
    
    all_stats = await self.get_all_stats()
    
    # Filter for suspicious/banned users
    problematic_users = {
        uid: stats for uid, stats in all_stats.items()
        if stats["banned"] or stats["suspicion_score"] > 50
    }
    
    report_lines = [
        "📊 Abuse Protection Report",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total tracked users: {len(all_stats)}",
        f"Problematic users: {len(problematic_users)}",
        ""
    ]
    
    for user_id, stats in sorted(
        problematic_users.items(),
        key=lambda x: x[1]["suspicion_score"],
        reverse=True
    ):
        report_lines.append(
            f"User {user_id}:\n"
            f"  Banned: {stats['banned']}\n"
            f"  Suspicion: {stats['suspicion_score']:.1f}\n"
            f"  Reputation: {stats['reputation']:.1f}\n"
            f"  Incidents: {stats['incidents']}"
        )
    
    return "\n".join(report_lines)

# Add to admin.py
@router.message(Command("abuse_report"))
async def cmd_abuse_report(
    message: Message,
    abuse_protector: "AbuseProtector | None" = None,
) -> None:
    """Generate abuse protection report."""
    if abuse_protector is None:
        await message.answer("ℹ️ Abuse protection is not enabled")
        return
    
    report = await abuse_protector.generate_abuse_report()
    
    # Handle long messages
    if len(report) > 4096:
        # Split into multiple messages
        for i in range(0, len(report), 4000):
            chunk = report[i:i+4000]
            await message.answer(f"```\n{chunk}\n```", parse_mode="HTML")
    else:
        await message.answer(f"```\n{report}\n```", parse_mode="HTML")
```

## Monitoring and Debugging

### Enable Debug Logging

```yaml
# jaato-client-telegram.yaml
logging:
  level: "DEBUG"
```

### Log Messages to Monitor

```
# Abuse protection initialization
AbuseProtector initialized: max_rapid=5, interval=3s

# Detection events
Creating permission keyboard with 10 options: [...]
Filtering unsupported actions: [...]

# Abuse events
Abuse warning for user 123456789: [{'type': 'rapid_messages', ...}]
Temporary ban applied to user 123456789: 300s - Abuse pattern detected
Permanent ban applied to user 987654321: TOS violation

# Admin actions
Admin: Temporary ban applied to user 123456789: 600s - Manual ban
Admin: User 123456789 unbanned

# Cleanup
Cleaned up 15 old abuse protection states
```

### Key Metrics to Track

1. **Ban Rate:** Number of bans per day
2. **Suspicion Distribution:** Average suspicion score across users
3. **Reputation Distribution:** User reputation levels
4. **Incident Types:** Most common abuse patterns
5. **Escalation Rate:** Warnings → Bans ratio

## Best Practices

### 1. Tuning Thresholds

**Start Conservative:**
```yaml
abuse_protection:
  suspicion_threshold: 80      # High threshold
  reputation_threshold: 10.0   # Very low threshold
  temporary_ban_duration: 300   # 5 minutes
```

**Monitor for 1-2 weeks:**
- Check `/abuse_stats` regularly
- Review ban reasons
- Get user feedback

**Adjust Based on Data:**
- Too many false positives → Increase thresholds
- Too much abuse → Decrease thresholds
- Balance between protection and usability

### 2. Admin Communication

**Inform Users:**
```
📜 Abuse Protection Policy

To prevent spam and abuse, this bot monitors for:
- Rapid messaging
- Repetitive content
- Suspicious patterns

Consequences:
1️⃣ Warning (first offense)
2️⃣ Temporary ban (5 minutes)
3️⃣ Permanent ban (severe/repeat offenses)

Questions? Contact: @admin_username
```

### 3. Regular Reviews

**Weekly:**
- Review `/abuse_stats`
- Check ban reasons
- Identify false positives

**Monthly:**
- Adjust thresholds based on data
- Review trusted user list
- Update abuse detection rules

### 4. Backup and Restore

**Export Abuse State:**

```python
import json

async def export_abuse_state(abuse_protector):
    stats = await abuse_protector.get_all_stats()
    with open('abuse_state.json', 'w') as f:
        json.dump(stats, f, indent=2)
```

**Import Abuse State:**

```python
async def import_abuse_state(abuse_protector, filepath):
    with open(filepath, 'r') as f:
        stats = json.load(f)
    
    # Restore state (simplified - actual implementation would need more)
    for user_id, user_stats in stats.items():
        state = UserAbuseState()
        # ... restore state fields ...
        abuse_protector._states[user_id] = state
```

## Troubleshooting

### Issue: Too Many False Positives

**Symptoms:** Legitimate users getting banned

**Solutions:**
1. Increase `suspicion_threshold` (try 80-90)
2. Increase `reputation_threshold` (try 20-30)
3. Increase `max_rapid_messages` (try 8-10)
4. Add trusted users to bypass list

### Issue: Abuse Still Occurring

**Symptoms:** Spam/bot accounts not being caught

**Solutions:**
1. Decrease `suspicion_threshold` (try 50-60)
2. Decrease `max_rapid_messages` (try 3-5)
3. Decrease `rapid_message_interval` (try 1-2)
4. Add custom detection rules (see examples)

### Issue: Users Stay Banned Too Long

**Symptoms:** Temporary bans feel too long

**Solutions:**
1. Decrease `temporary_ban_duration` (try 60-180s)
2. Implement dynamic duration based on offense count
3. Manually unban users if needed

## Related Documentation

- [README.md](README.md) - Configuration and setup
- [CHANGELOG.md](CHANGELOG.md) - Version history
- [ROADMAP.md](ROADMAP.md) - Development roadmap
- [handlers/admin.py](src/jaato_client_telegram/handlers/admin.py) - Admin commands
