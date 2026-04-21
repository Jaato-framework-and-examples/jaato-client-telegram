# Telemetry System - Implementation Documentation

## Overview

The jaato-client-telegram bot includes a **minimal bot-layer telemetry system** that collects metrics which are unique to the bot layer and do not duplicate metrics already collected by jaato-server.

## Design Philosophy

### Why Minimal Telemetry?

**jaato-server already provides:**
- Agent execution metrics (tool calls, reasoning time)
- Session metrics (memory usage, waypoints)
- Performance metrics (LLM latency, token usage)
- Error tracking and logging

**Bot-layer needs:**
- Telegram API health and delivery metrics
- User interaction patterns (buttons, commands)
- Client-side connection health
- Rate limiting and abuse protection effectiveness
- End-to-end latency from user → bot → jaato → bot → user

### What We DON'T Collect

We intentionally **DO NOT** collect:
- LLM-related metrics (jaato-server has this)
- Tool usage details (jaato-server has this)
- Token counts (jaato-server has this)
- Agent reasoning time (jaato-server has this)
- Duplicate business intelligence

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────────┐
│                    TelemetryCollector                    │
│  - TelegramDeliveryMetrics                               │
│  - UIInteractionMetrics                                    │
│  - SessionPoolMetrics                                     │
│  - RateLimitingMetrics                                   │
│  - AbuseProtectionMetrics                                 │
│  - LatencyMetrics                                       │
└─────────────────────────────────────────────────────────────┘
         │
         ├─► Record methods (called from handlers)
         ├─► Summary generation (for display)
         └─► Automatic cleanup (old data)
```

### Data Structures

#### TelegramDeliveryMetrics

```python
@dataclass
class TelegramDeliveryMetrics:
    messages_sent: int                 # Successful message sends
    messages_failed: int               # Failed message sends
    errors_last_hour: list[str]        # Error messages (last hour)
    last_error: Optional[str]           # Most recent error
    last_error_time: Optional[float]     # Timestamp of last error
```

**Purpose:** Monitor Telegram API health and detect issues with the bot's connection to Telegram.

**Recording:**
```python
# When message sent successfully
telemetry.record_message_sent()

# When message send fails
telemetry.record_message_failed("Timeout waiting for Telegram API")
```

---

#### UIInteractionMetrics

```python
@dataclass
class UIInteractionMetrics:
    permission_approvals: int          # Permission approvals
    permission_denials: int            # Permission denials
    command_usage: dict[str, int]        # Command → count
    button_clicks: dict[str, int]         # Button type → count
    collapsible_expands: int            # Expandable content expanded
    message_edits: int                 # Message edit operations
```

**Purpose:** Understand how users interact with the bot and identify popular features.

**Recording:**
```python
# Permission decision
telemetry.record_permission_approval()
telemetry.record_permission_denial()

# Command usage
telemetry.record_command_usage("/start")

# Button click
telemetry.record_button_click("allow")

# Message edit (streaming updates)
telemetry.record_message_edit()
```

---

#### SessionPoolMetrics

```python
@dataclass
class SessionPoolMetrics:
    active_connections: int           # Currently connected sessions
    max_connections: int              # Configured maximum
    connection_errors: int            # Connection errors
    disconnections: int               # Session disconnections
    avg_session_duration: float      # Average session duration (seconds)
    session_durations: deque          # Recent session durations
```

**Purpose:** Monitor session pool health and detect connection issues.

**Recording:**
```python
# When session created
telemetry.record_session_created()

# When session ends (with duration)
telemetry.record_session_ended(duration_seconds)

# On connection error
telemetry.record_connection_error()
```

---

#### RateLimitingMetrics

```python
@dataclass
class RateLimitingMetrics:
    users_limited: int                # Total users rate-limited
    cooldowns_triggered: int          # Total cooldowns triggered
    most_limited_user: Optional[int] # Most frequently limited user
    limit_hits_last_hour: dict[int, int]  # User → hit count (last hour)
```

**Purpose:** Measure rate limiting effectiveness and identify problematic users.

**Recording:**
```python
# When user hits rate limit
telemetry.record_rate_limit_hit(user_id)

# When cooldown is triggered
telemetry.record_cooldown_triggered()
```

---

#### AbuseProtectionMetrics

```python
@dataclass
class AbuseProtectionMetrics:
    bans_applied: int                # Total bans applied
    temporary_bans: int              # Temporary bans count
    permanent_bans: int              # Permanent bans count
    warnings_issued: int             # Warning messages sent
    suspicious_users: int             # Users flagged as suspicious
    unban_operations: int           # Manual unbans
```

**Purpose:** Track abuse detection effectiveness and ban trends.

**Recording:**
```python
# When ban is applied
telemetry.record_ban_applied("temporary")
telemetry.record_ban_applied("permanent")

# When warning is issued
telemetry.record_warning_issued()

# When suspicious user detected
telemetry.record_suspicious_user()

# When admin unbans user
telemetry.record_unban_operation()
```

---

#### LatencyMetrics

```python
@dataclass
class LatencyMetrics:
    request_count: int                # Total requests tracked
    total_latency: float              # Sum of all latencies
    avg_latency: float               # Average latency (ms)
    p50_latency: float               # 50th percentile (ms)
    p95_latency: float               # 95th percentile (ms)
    p99_latency: float               # 99th percentile (ms)
    latencies: deque                  # Last 1000 latencies
```

**Purpose:** Track end-to-end latency from user message to bot response.

**Recording:**
```python
# Record latency in milliseconds
telemetry.record_latency(1234)  # 1.234 seconds
```

**Calculation:**
```python
# Percentiles are calculated from the last 1000 requests
sorted_latencies = sorted(latencies)
p50 = sorted_latencies[int(n * 0.5)]
p95 = sorted_latencies[int(n * 0.95)]
p99 = sorted_latencies[int(n * 0.99)]
```

---

## Integration Points

### 1. Bot Initialization

**File:** `bot.py`

```python
# Create telemetry collector
telemetry: TelemetryCollector | None = None
if config.telemetry.enabled:
    telemetry = TelemetryCollector(config.telemetry)
    logger.info("Telemetry enabled")

# Inject into dispatcher
dp["telemetry"] = telemetry
```

### 2. Admin Commands

**File:** `handlers/admin.py`

```python
@router.message(Command("telemetry"))
async def cmd_telemetry(
    message: Message,
    telemetry: "TelemetryCollector | None" = None,
) -> None:
    """Show telemetry statistics (admin command)."""
    if telemetry is None:
        await message.answer("ℹ️ Telemetry is not enabled")
        return
    
    # Get and display summary
    summary = await telemetry.get_summary()
    # ... format and send ...
```

### 3. Future Integration Points

To integrate telemetry recording into the actual message handlers, you would:

```python
# In private.py or group.py
async def handle_private_message(
    message: Message,
    telemetry: "TelemetryCollector | None" = None,
    # ... other dependencies ...
) -> None:
    # Before processing
    if telemetry:
        telemetry.record_command_usage("message")
    
    # ... process message ...
    
    # After sending response
    if telemetry:
        telemetry.record_message_sent()
        telemetry.record_latency(latency_ms)
```

## Configuration

### Full Configuration

```yaml
# jaato-client-telegram.yaml
telemetry:
  # Enable/disable entire telemetry system
  enabled: false
  
  # Individual metric categories (can be enabled/disabled)
  collect_telegram_delivery: true
  collect_ui_interactions: true
  collect_session_pool: true
  collect_rate_limiting: true
  collect_abuse_protection: true
  collect_latency: true
  
  # Data retention
  retention_hours: 24           # How long to keep metrics
  
  # Cleanup
  cleanup_interval_minutes: 60   # How often to clean old data
```

### Configuration Tips

**Development:**
```yaml
telemetry:
  enabled: false  # Off during dev to avoid noise
```

**Production:**
```yaml
telemetry:
  enabled: true
  retention_hours: 168  # Keep 7 days
  cleanup_interval_minutes: 60
```

**Minimal Monitoring:**
```yaml
telemetry:
  enabled: true
  collect_telegram_delivery: true    # Only delivery metrics
  collect_ui_interactions: false     # Skip UI tracking
  collect_session_pool: false        # Skip session tracking
  collect_rate_limiting: false      # Skip rate limit tracking
  collect_abuse_protection: false    # Skip abuse tracking
  collect_latency: true              # Track latency
```

## Admin Command

### `/telemetry`

Shows a comprehensive telemetry summary with sections:

```bash
/telemetry
```

**Output Format:**

```
📊 Telemetry Statistics

Uptime: 2.3 hours

📤 Telegram API:
  Sent: 142
  Failed: 3
  Error rate: 2.1%
  Errors (1h): 2

🖱️ UI Interactions:
  Permissions: 15✅ / 3❌
  Message edits: 47
  Collapsible expands: 8
  Top commands: /start, /reset, /help

🔗 Session Pool:
  Active: 5/50
  Utilization: 10.0%
  Errors: 0
  Avg session: 45.2s

⏱️ Rate Limiting:
  Users limited: 2
  Cooldowns: 5
  Active limited: 1

🛡️ Abuse Protection:
  Bans applied: 3
  Temporary: 2
  Permanent: 1
  Warnings: 7

⚡ Latency (end-to-end):
  Avg: 2345ms
  P50: 1980ms
  P95: 4120ms
  P99: 5780ms
  Requests: 142
```

## Data Management

### Cleanup

Automatic cleanup removes old data:

```python
async def cleanup_old_data(self, max_age_hours: int = 24) -> None:
    """Clean up old telemetry data."""
    async with self._lock:
        now = time.time()
        
        # Clean old error records (older than retention period)
        if self._config.collect_telegram_delivery:
            cutoff = now - max_age_hours * 3600
            self._telegram.errors_last_hour = [
                e for e in self._telegram.errors_last_hour
                if now - (self._start_time + e["time"]) < cutoff
            ]
        
        # Clean old rate limit hits (older than retention period)
        if self._config.collect_rate_limiting:
            # Clear limit hits that are too old
            # Implementation removes entries older than retention period
            pass
```

### Retention Policy

**Default:** 24 hours

**Considerations:**
- **Storage:** Metrics use minimal memory (deques with maxlen)
- **Privacy:** No user content stored, only counts and stats
- **Performance:** O(1) recording operations (lock-free for most)
- **Cleanup:** Automatic, configurable intervals

## Performance Impact

### Memory Usage

Per metric type:
- **TelegramDeliveryMetrics:** ~200 bytes
- **UIInteractionMetrics:** ~500 bytes + dynamic dicts
- **SessionPoolMetrics:** ~400 bytes + deque(100) floats
- **RateLimitingMetrics:** ~300 bytes + dynamic dict
- **AbuseProtectionMetrics:** ~200 bytes
- **LatencyMetrics:** ~400 bytes + deque(1000) floats

**Total:** ~2-3 KB per active telemetry session

### CPU Impact

Recording operations are O(1) or O(log n):
- Counter increments: O(1)
- Dictionary lookups: O(1) amortized
- Deque operations: O(1)
- Percentile calculation: O(n log n) for n requests (capped at 1000)

**Latency:** Negligible (< 1ms per recording operation)

### Lock Contention

Most operations are lock-free (atomic increments):
- Counter updates use no lock
- Deque operations are thread-safe

Lock only used for:
- Summary generation (infrequent)
- Cleanup operations (periodic)

## Extending Telemetry

### Adding a New Metric Type

1. **Define dataclass:**

```python
@dataclass
class NewMetric:
    count: int = 0
    details: dict = field(default_factory=dict)
```

2. **Add to TelemetryCollector:**

```python
class TelemetryCollector:
    def __init__(self, config: TelemetryConfig) -> None:
        # ... existing metrics ...
        self._new_metric: NewMetric = NewMetric()
```

3. **Add recording method:**

```python
def record_new_metric(self, value: str) -> None:
    """Record a new metric."""
    if self._config.collect_new_metric:
        self._new_metric.count += 1
```

4. **Add to summary:**

```python
async def get_summary(self) -> dict:
    return {
        # ... existing metrics ...
        "new_metric": {
            "count": self._new_metric.count,
            "details": self._new_metric.details,
        } if self._config.collect_new_metric else None
    }
```

5. **Update config:**

```python
class TelemetryConfig(BaseModel):
    # ... existing fields ...
    collect_new_metric: bool = True
```

### Example: Add Command Usage Per User

```python
@dataclass
class UserCommandMetrics:
    user_commands: dict[int, dict[str, int]]  # user_id -> {command: count}
    total_commands: int = 0

# In TelemetryCollector
def record_user_command(self, user_id: int, command: str) -> None:
    if self._config.collect_user_commands:
        if user_id not in self._user_commands:
            self._user_commands[user_id] = {}
        
        user_commands = self._user_commands[user_id]
        user_commands[command] = user_commands.get(command, 0) + 1
        self._user_commands.total_commands += 1
```

## Best Practices

### 1. Keep It Minimal

**DO:**
- Collect only what you need to answer specific questions
- Focus on bot-layer concerns, not agent concerns
- Use simple data structures (no complex objects)

**DON'T:**
- Collect user message content
- Track detailed request/response payloads
- Duplicate metrics jaato-server already has

### 2. Performance First

**DO:**
- Use atomic operations for counters
- Use bounded collections (deque with maxlen)
- Avoid locks for simple operations
- Batch expensive calculations (percentiles)

**DON'T:**
- Lock on every record operation
- Collect unbounded data structures
- Do expensive processing inline

### 3. Privacy Respectful

**DO:**
- Store only aggregated data (counts, statistics)
- No PII or message content
- Short retention periods
- Clear documentation on what's collected

**DON'T:**
- Log user messages
- Track user identity beyond user_id
- Store any conversation history

### 4. Actionable Insights

**DO:**
- Collect metrics that help you make decisions
- Focus on actionable data
- Present summaries clearly

**DON'T:**
- Collect everything "just in case"
- Store data without knowing how you'll use it
- Over-collect without clear purpose

## Troubleshooting

### Telemetry Not Recording

**Symptom:** `/telemetry` shows all zeros

**Check:**
1. Is telemetry enabled in config?
   ```yaml
   telemetry:
     enabled: true
   ```

2. Are recording methods being called?
   - Add debug logs to verify

3. Is handler injected into dispatcher?
   - Check `dp["telemetry"]` in bot.py

### Missing Metrics

**Symptom:** Some metrics not showing in `/telemetry`

**Check:**
1. Is the metric category enabled in config?
   ```yaml
   collect_session_pool: true  # Must be enabled
   ```

2. Are recording methods being called?
   - Add telemetry recording calls in handlers

### High Memory Usage

**Symptom:** Memory usage growing over time

**Check:**
1. Are bounded collections being used?
   - Check deque maxlen values
   - Ensure old data is being cleaned up

2. Is retention period appropriate?
   - Reduce `retention_hours` if needed

## Examples

### Example 1: Track Message Delivery Rate

**Scenario:** Monitor how many messages fail to send

```python
# In message handler
try:
    await message.answer(text)
    if telemetry:
        telemetry.record_message_sent()
except TelegramAPIError as e:
    if telemetry:
        telemetry.record_message_failed(str(e))
```

### Example 2: Track Session Duration

**Scenario:** Understand how long user sessions last

```python
# Record session start
async def handle_private_message(...):
    if telemetry:
        telemetry.record_session_created()
    
    # ... process message ...

# Record session end (somewhere else)
telemetry.record_session_ended(duration_seconds)
```

### Example 3: Track Latency Distribution

**Scenario:** Understand latency percentiles

```python
# Record latency for each request
telemetry.record_latency(end_time - start_time)

# View distribution
summary = await telemetry.get_summary()
latency = summary["latency"]
print(f"Avg: {latency['avg_latency_ms']}ms")
print(f"P50: {latency['p50_latency_ms']}ms")
print(f"P95: {latency['p95_latency_ms']}ms")
print(f"P99: {latency['p99_latency_ms']}ms")
```

### Example 4: Custom Metric: Error Types

**Scenario:** Track error types for debugging

```python
# Add to TelemetryCollector
_error_counts: dict[str, int] = field(default_factory=dict)

# Add recording method
def record_error(self, error_type: str) -> None:
    self._error_counts[error_type] = self._error_counts.get(error_type, 0) + 1

# Add to summary
"error_counts": dict(sorted(
    self._error_counts.items(),
    key=lambda x: x[1],
    reverse=True
)[:10])  # Top 10 error types
```

## Related Documentation

- [README.md](README.md) - Telemetry configuration
- [config.example.yaml](config.example.yaml) - Configuration reference
- [CHANGELOG.md](CHANGELOG.md) - Version history
- [ROADMAP.md](ROADMAP.md) - Development roadmap
