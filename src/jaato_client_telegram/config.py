"""
Configuration management for jaato-client-telegram.

Loads client configuration from YAML file with environment variable substitution.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TelegramWebhookConfig(BaseModel):
    """Webhook configuration for Telegram bot."""

    url: str
    host: str = "0.0.0.0"
    port: int = 8443
    path: str = "/tg-webhook"
    secret_token: str | None = None
    cert_path: str | None = None
    max_connections: int = 40
    allowed_updates: list[str] | None = None


class TelegramAccessConfig(BaseModel):
    """Access control for Telegram bot."""

    allowed_chat_ids: list[int] = Field(default_factory=list)
    admin_user_ids: list[int] = Field(default_factory=list)


class TelegramGroupConfig(BaseModel):
    """Group chat behavior configuration."""

    require_mention: bool = True
    trigger_prefix: str | None = None


class TelegramConfig(BaseModel):
    """Telegram-side configuration."""

    bot_token: str
    mode: Literal["polling", "webhook"] = "polling"
    webhook: TelegramWebhookConfig | None = None
    access: TelegramAccessConfig = Field(default_factory=TelegramAccessConfig)
    group: TelegramGroupConfig = Field(default_factory=TelegramGroupConfig)


class TLSConfig(BaseModel):
    """TLS configuration for WebSocket transport."""

    enabled: bool = False
    cert_path: str | None = None
    key_path: str | None = None
    ca_cert_path: str | None = None


class JaatoWSConfig(BaseModel):
    """jaato WebSocket transport configuration."""

    url: str = "ws://localhost:8080"
    tls: TLSConfig = Field(default_factory=TLSConfig)
    secret_token: str | None = None
    workspace_template: str = "default"
    keycloak_base_url: str = ""
    keycloak_realm: str = "jaato"
    keycloak_client_id: str = ""
    keycloak_client_secret: str = ""


class SessionConfig(BaseModel):
    """Session pool configuration."""

    max_concurrent: int = 50
    idle_timeout_minutes: int = 60
    reconnect_on_error: bool = True


class PermissionConfig(BaseModel):
    """Permission request UI configuration."""

    # Comma-separated list of unsupported action types to filter from inline keyboard
    # Example: "comment,edit,idle,turn,all" or use pipe: "comment|edit|idle|turn|all"
    unsupported_actions: str = "comment,edit,modify,custom,input"


class RenderingConfig(BaseModel):
    """Response rendering configuration."""

    max_message_length: int = 4096
    stream_edits: bool = True
    typing_indicator: bool = True
    edit_throttle_ms: int = 500


class RateLimitingConfig(BaseModel):
    """Rate limiting configuration."""

    enabled: bool = False
    messages_per_minute: int = 30
    messages_per_hour: int = 200
    cooldown_seconds: int = 60
    admin_bypass: bool = True
    cleanup_interval_minutes: int = 60
    cleanup_max_age_hours: int = 24


class AbuseProtectionConfig(BaseModel):
    """Abuse protection configuration."""

    enabled: bool = False
    max_rapid_messages: int = 5
    rapid_message_interval: int = 3
    suspicion_threshold: int = 70
    reputation_threshold: float = 30.0
    temporary_ban_duration: int = 300
    admin_bypass: bool = True
    cleanup_interval_minutes: int = 60
    cleanup_max_age_hours: int = 24


class TelemetryConfig(BaseModel):
    """Minimal bot-layer telemetry configuration."""

    enabled: bool = False
    collect_telegram_delivery: bool = True
    collect_ui_interactions: bool = True
    collect_session_pool: bool = True
    collect_rate_limiting: bool = True
    collect_abuse_protection: bool = True
    collect_latency: bool = True
    retention_hours: int = 24
    cleanup_interval_minutes: int = 60


class FileSharingConfig(BaseModel):
    """File sharing configuration for generated files."""

    enabled: bool = True
    max_file_size_mb: int = 10  # Telegram bot limit
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".txt", ".csv", ".json", ".xml", ".yaml", ".yml", ".md", ".py", ".js", ".ts", ".html", ".css"]
    )
    # Size threshold for determining delivery method (in KB)
    # Files < this size are sent as document attachments
    # Files >= this size are sent as Telegram file hosting URLs
    link_threshold_kb: int = 100  # 100KB default threshold


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    format: Literal["structured", "text"] = "structured"


class Config(BaseModel):
    """Root configuration for jaato-client-telegram."""

    telegram: TelegramConfig
    jaato_ws: JaatoWSConfig = Field(default_factory=JaatoWSConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    rendering: RenderingConfig = Field(default_factory=RenderingConfig)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    rate_limiting: RateLimitingConfig = Field(default_factory=RateLimitingConfig)
    abuse_protection: AbuseProtectionConfig = Field(default_factory=AbuseProtectionConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    file_sharing: FileSharingConfig = Field(default_factory=FileSharingConfig)


def _substitute_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable value."""
    import os
    import re

    pattern = re.compile(r"\$\{([^}]+)\}")

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return pattern.sub(replacer, value)


def _substitute_dict(data: dict) -> dict:
    """Recursively substitute environment variables in dict values."""
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = _substitute_env_vars(value)
        elif isinstance(value, dict):
            result[key] = _substitute_dict(value)
        elif isinstance(value, list):
            result[key] = [
                _substitute_env_vars(item) if isinstance(item, str) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def load_config(path: str | None = None) -> Config:
    """
    Load configuration from YAML file.

    Args:
        path: Path to config file. If None, looks for jaato-client-telegram.yaml
              in current directory, or ./config/jaato-client-telegram.yaml

    Returns:
        Validated Config object

    Raises:
        FileNotFoundError: If config file not found
        ValidationError: If config fails Pydantic validation
    """
    if path is None:
        # Try default locations
        candidates = [
            Path("jaato-client-telegram.yaml"),
            Path("config/jaato-client-telegram.yaml"),
        ]
        for candidate in candidates:
            if candidate.exists():
                path = str(candidate)
                break
        else:
            raise FileNotFoundError(
                "Config file not found. Please create jaato-client-telegram.yaml "
                "or specify path with --config"
            )

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with config_path.open() as f:
        raw_data = yaml.safe_load(f)

    # Substitute environment variables
    data = _substitute_dict(raw_data)

    return Config(**data)
