# Testing Guide for jaato-client-telegram

This document describes how to test jaato-client-telegram.

## Unit Tests

Unit tests validate individual components without requiring a running jaato server.

### Running Unit Tests

```bash
# From the project root
cd jaato-client-telegram

# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=src/jaato_client_telegram --cov-report=html

# Run specific test
pytest tests/test_client.py::TestResponseRenderer::test_split_short_text -v
```

### Unit Test Coverage

- **ResponseRenderer**: Message splitting, paragraph preservation
- **SessionPool**: Client eviction, idle cleanup
- **Config**: Environment variable substitution, defaults

## Integration Tests

Integration tests require a running jaato server. These are currently manual.

### Manual Integration Testing

#### Prerequisites

1. **jaato server running**:
   ```bash
   cd /path/to/jaato
   python -m jaato
   ```
   Verify socket exists: `ls -l /tmp/jaato.sock`

2. **Telegram bot token**:
   ```bash
   export TELEGRAM_BOT_TOKEN="your-bot-token-from-botfather"
   ```

3. **Client configured**:
   ```bash
   cp config.example.yaml jaato-client-telegram.yaml
   # Edit if needed (socket path, etc.)
   ```

#### Test Scenarios

**1. Basic Message Flow**

```
User: /start
Bot: ✅ Connected to jaato! ...

User: Hello!
Bot: [Streams response progressively]
```

Verify:
- ✅ Session created in pool
- ✅ Message sent to jaato
- ✅ Events streamed back
- ✅ Response rendered progressively

**2. Session Isolation**

```
User A: /start
User A: What is your name?
Bot A: [Response with session context]

User B: /start
User B: What is your name?
Bot B: [Different response, separate session]
```

Verify:
- ✅ Each user gets separate SDK client
- ✅ Sessions are isolated
- ✅ Status shows 2 active sessions

**3. Long Message Handling**

```
User: Tell me a very long story with lots of details
Bot: [Splits into multiple messages if >4096 chars]
```

Verify:
- ✅ Long responses split at paragraph boundaries
- ✅ Each chunk <= 4096 chars
- ✅ All chunks sent in order

**4. Session Reset**

```
User: Remember my name is Alice
Bot: OK Alice, I'll remember that.

User: /reset
Bot: 🔄 Session reset.

User: What is my name?
Bot: [Doesn't remember - session cleared]
```

Verify:
- ✅ Old client disconnected
- ✅ New client created on next message
- ✅ Session state cleared

**5. Idle Cleanup**

```
User: /start
[Wait 60+ minutes]

[Background task should cleanup session]

User: /status
Bot: Active sessions: 0
```

Verify:
- ✅ Idle sessions cleaned up
- ✅ Resources freed
- ✅ New session created on next message

**6. Error Handling**

```
[Stop jaato server]

User: Hello!
Bot: ❌ Failed to connect to jaato server.

[Start jaato server]

User: /reset
Bot: 🔄 Session reset.

User: Hello!
Bot: [Normal response]
```

Verify:
- ✅ Graceful error message
- ✅ Recovery after /reset
- ✅ No crashes or hangs

## Debugging

### Enable Debug Logging

Edit `jaato-client-telegram.yaml`:

```yaml
logging:
  level: "DEBUG"
  format: "text"  # Easier to read than JSON
```

### Check SDK Client Status

Add logging in `session_pool.py`:

```python
logger.debug(f"Active sessions: {self.active_count}")
logger.debug(f"Chat {chat_id}: session={session_info}")
```

### Monitor jaato Server

jaato server logs will show:
- IPC connections from clients
- Messages received
- Events sent

### Test Message Flow with Mock

Create a test script that mocks the SDK:

```python
import asyncio
from unittest.mock import AsyncMock

async def test_message_flow():
    # Create mock SDK client
    mock_client = AsyncMock()
    mock_client.events.return_value = [
        Mock(type="AGENT_OUTPUT", content="Hello"),
        Mock(type="AGENT_OUTPUT", content=" World"),
    ]

    # Test renderer with mock stream
    from jaato_client_telegram.renderer import ResponseRenderer
    renderer = ResponseRenderer()

    # ... test logic
```

## Troubleshooting Tests

### ImportError: No module named 'jaato_sdk'

Install dependencies:
```bash
pip install -e .
```

### Tests Hang Indefinitely

- Check if jaato server is running
- Verify socket path in config
- Check firewall/permissions

### Session Pool Tests Fail

- Ensure mock setup is correct
- Check async/await usage
- Verify asyncio event loop

## CI/CD

Future work:
- GitHub Actions workflow
- Automated integration tests with test jaato server
- Code coverage reporting
- Linting (black, ruff, mypy)
