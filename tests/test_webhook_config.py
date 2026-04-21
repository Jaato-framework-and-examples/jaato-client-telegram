"""
Test webhook configuration validation.

Tests that webhook mode configuration is properly loaded and validated.
"""

import pytest
import tempfile
from pathlib import Path

from jaato_client_telegram.config import load_config


def test_webhook_config_validation():
    """Test that webhook configuration is properly validated."""
    # Create a temporary config file with webhook settings
    config_yaml = """
telegram:
  bot_token: "test-token"
  mode: "webhook"
  webhook:
    url: "https://example.com/tg-webhook"
    host: "0.0.0.0"
    port: 8443
    path: "/tg-webhook"
    secret_token: "test-secret-token"
    max_connections: 40
    allowed_updates: ["message", "callback_query"]

jaato:
  socket_path: "/tmp/jaato.sock"
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_path = f.name

    try:
        # Load config
        config = load_config(config_path)

        # Verify webhook mode is set
        assert config.telegram.mode == "webhook"

        # Verify webhook configuration is present
        assert config.telegram.webhook is not None

        # Verify webhook settings
        webhook = config.telegram.webhook
        assert webhook.url == "https://example.com/tg-webhook"
        assert webhook.host == "0.0.0.0"
        assert webhook.port == 8443
        assert webhook.path == "/tg-webhook"
        assert webhook.secret_token == "test-secret-token"
        assert webhook.max_connections == 40
        assert webhook.allowed_updates == ["message", "callback_query"]
        assert webhook.cert_path is None

    finally:
        # Clean up
        Path(config_path).unlink()


def test_polling_mode_config():
    """Test that polling mode configuration works without webhook."""
    config_yaml = """
telegram:
  bot_token: "test-token"
  mode: "polling"

jaato:
  socket_path: "/tmp/jaato.sock"
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_path = f.name

    try:
        # Load config
        config = load_config(config_path)

        # Verify polling mode is set
        assert config.telegram.mode == "polling"

        # Verify webhook configuration is None
        assert config.telegram.webhook is None

    finally:
        # Clean up
        Path(config_path).unlink()


def test_webhook_defaults():
    """Test that webhook configuration has sensible defaults."""
    config_yaml = """
telegram:
  bot_token: "test-token"
  mode: "webhook"
  webhook:
    url: "https://example.com/tg-webhook"

jaato:
  socket_path: "/tmp/jaato.sock"
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_path = f.name

    try:
        # Load config
        config = load_config(config_path)

        # Verify default values
        webhook = config.telegram.webhook
        assert webhook.host == "0.0.0.0"
        assert webhook.port == 8443
        assert webhook.path == "/tg-webhook"
        assert webhook.secret_token is None
        assert webhook.max_connections == 40
        assert webhook.allowed_updates is None
        assert webhook.cert_path is None

    finally:
        # Clean up
        Path(config_path).unlink()


def test_webhook_url_required():
    """Test that webhook URL is required when mode=webhook."""
    config_yaml = """
telegram:
  bot_token: "test-token"
  mode: "webhook"
  webhook:
    host: "0.0.0.0"
    port: 8443

jaato:
  socket_path: "/tmp/jaato.sock"
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_path = f.name

    try:
        # This should raise a validation error (url is required)
        with pytest.raises(Exception):  # Pydantic ValidationError
            load_config(config_path)

    finally:
        # Clean up
        Path(config_path).unlink()
