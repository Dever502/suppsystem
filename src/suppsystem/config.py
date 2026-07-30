from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_INTEGRATION_SECRET_LENGTH = 32
PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me",
        "changeme",
        "placeholder",
        "replace-me",
        "secret",
        "test-secret",
        "test-token",
        "your-secret",
        "your-token",
    }
)
PLACEHOLDER_SECRET_PARTS = (
    "change-me",
    "placeholder",
    "replace-me",
    "secret",
    "test-secret",
    "test-token",
    "your-secret",
    "your-token",
)
POSTGRES_PLACEHOLDER_PASSWORDS = PLACEHOLDER_SECRETS | frozenset({"postgres", "suppsystem"})
MIN_DATABASE_PASSWORD_LENGTH = 16
HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _is_valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return False
    return (
        bool(ascii_hostname)
        and len(ascii_hostname) <= 253
        and all(HOST_LABEL_PATTERN.fullmatch(label) for label in ascii_hostname.split("."))
    )


def _is_loopback_hostname(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_external_url(variable_name: str, value: str) -> None:
    """Reject unsafe integration URLs without including their value in errors."""

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError as error:
        raise ValueError(f"{variable_name} must be a valid absolute URL") from error
    if parsed.scheme.casefold() not in {"http", "https"} or hostname is None:
        raise ValueError(f"{variable_name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{variable_name} must not contain credentials")
    if parsed.fragment:
        raise ValueError(f"{variable_name} must not contain a URL fragment")
    if not _is_valid_hostname(hostname):
        raise ValueError(f"{variable_name} must contain a valid hostname")
    if parsed.scheme.casefold() == "http" and not _is_loopback_hostname(hostname):
        raise ValueError(f"{variable_name} must use HTTPS outside loopback development")


def validate_enabled_secret(variable_name: str, value: SecretStr) -> None:
    """Validate an enabled integration secret without exposing it in errors."""

    raw_value = value.get_secret_value()
    normalized_value = raw_value.casefold()
    is_placeholder = normalized_value in PLACEHOLDER_SECRETS or any(
        not normalized_value.replace(part, "").strip("-_. ") for part in PLACEHOLDER_SECRET_PARTS
    )
    if (
        raw_value != raw_value.strip()
        or len(raw_value) < MIN_INTEGRATION_SECRET_LENGTH
        or is_placeholder
    ):
        raise ValueError(
            f"{variable_name} must be a non-placeholder secret of at least "
            f"{MIN_INTEGRATION_SECRET_LENGTH} characters"
        )


def validate_database_url_secret(variable_name: str, value: str) -> None:
    """Reject weak PostgreSQL credentials without exposing the URL."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return
    if not parsed.scheme.casefold().startswith(("postgresql", "postgres")):
        return
    password = parsed.password
    if password is None:
        return
    normalized_value = password.casefold()
    is_placeholder = normalized_value in POSTGRES_PLACEHOLDER_PASSWORDS or any(
        not normalized_value.replace(part, "").strip("-_. ") for part in PLACEHOLDER_SECRET_PARTS
    )
    if (
        password != password.strip()
        or len(password) < MIN_DATABASE_PASSWORD_LENGTH
        or is_placeholder
    ):
        raise ValueError(
            f"{variable_name} PostgreSQL password must be a non-placeholder secret of at "
            f"least {MIN_DATABASE_PASSWORD_LENGTH} characters"
        )


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    support_bot_token: SecretStr
    support_group_id: int
    admin_telegram_ids: frozenset[int] = Field(default_factory=frozenset)
    data_dir: Path = Path("./data")
    database_url: str | None = None
    migration_database_url: str | None = None
    migrations_at_startup: bool = True
    log_level: str = "INFO"
    delivery_poll_interval_seconds: float = 1.0
    telegram_min_request_interval_seconds: float = 0.05
    telegram_inbound_rate_limit_per_minute: int = 30
    telegram_inbound_rate_limit_per_hour: int = 150
    delivery_max_attempts: int = 8
    api_enabled: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    api_admin_token: SecretStr | None = None
    api_unsafe_disable_auth: bool = False
    api_operator_telegram_id: int = 0
    api_rate_limit_requests: int = 120
    api_rate_limit_window_seconds: float = 60.0
    api_auth_failure_limit: int = 10
    api_auth_failure_window_seconds: float = 60.0
    api_trusted_proxy_ips: frozenset[str] = Field(default_factory=frozenset)
    remnawave_enabled: bool = False
    remnawave_base_url: str | None = None
    remnawave_api_token: SecretStr | None = None
    remnawave_timeout_seconds: float = 5.0
    remnawave_reconcile_delay_seconds: float = 10.0
    remnawave_revoke_link_telegram_notification: bool = True
    notification_webhook_enabled: bool = False
    notification_webhook_url: str | None = None
    notification_webhook_secret: SecretStr | None = None
    notification_webhook_timeout_seconds: float = 5.0
    notification_webhook_max_attempts: int = 8
    notification_webhook_poll_interval_seconds: float = 1.0

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def parse_telegram_ids(cls, value: object) -> frozenset[int]:
        if value is None or value == "":
            return frozenset()
        if isinstance(value, str):
            return frozenset(int(part.strip()) for part in value.split(",") if part.strip())
        if isinstance(value, int) and not isinstance(value, bool):
            return frozenset({value})
        if isinstance(value, (set, frozenset, list, tuple)):
            return frozenset(int(item) for item in value)
        raise ValueError(
            "Telegram IDs must be an integer, a comma-separated string, or a collection of integers"
        )

    @field_validator("api_trusted_proxy_ips", mode="before")
    @classmethod
    def parse_trusted_proxy_ips(cls, value: object) -> frozenset[str]:
        if value is None or value == "":
            return frozenset()
        if isinstance(value, str):
            raw_items = (part.strip() for part in value.split(","))
        elif isinstance(value, (set, frozenset, list, tuple)):
            raw_items = (str(item).strip() for item in value)
        else:
            raise ValueError("API_TRUSTED_PROXY_IPS must be a comma-separated list or collection")

        parsed: set[str] = set()
        for item in raw_items:
            if not item:
                continue
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as error:
                raise ValueError(
                    "API_TRUSTED_PROXY_IPS must contain IP addresses or CIDR networks"
                ) from error
            parsed.add(item)
        return frozenset(parsed)

    @model_validator(mode="after")
    def validate_runtime_settings(self) -> Self:
        if self.database_url is None:
            database_path = self.data_dir / "support.db"
            self.database_url = f"sqlite+aiosqlite:///{database_path}"
        validate_database_url_secret("DATABASE_URL", self.database_url)
        if self.migration_database_url is None:
            self.migration_database_url = self.database_url
        validate_database_url_secret("MIGRATION_DATABASE_URL", self.migration_database_url)
        if self.remnawave_base_url is not None:
            validate_external_url("REMNAWAVE_BASE_URL", self.remnawave_base_url)
        if self.notification_webhook_url is not None:
            validate_external_url("NOTIFICATION_WEBHOOK_URL", self.notification_webhook_url)
        if self.api_enabled and not self.api_unsafe_disable_auth:
            if self.api_admin_token is None:
                raise ValueError("API_ADMIN_TOKEN is required when API is enabled")
            validate_enabled_secret("API_ADMIN_TOKEN", self.api_admin_token)
        if self.remnawave_enabled and (not self.remnawave_base_url or not self.remnawave_api_token):
            raise ValueError(
                "REMNAWAVE_BASE_URL and REMNAWAVE_API_TOKEN are required when Remnawave is enabled"
            )
        if self.remnawave_enabled:
            assert self.remnawave_api_token is not None
            validate_enabled_secret("REMNAWAVE_API_TOKEN", self.remnawave_api_token)
        if self.remnawave_timeout_seconds <= 0:
            raise ValueError("REMNAWAVE_TIMEOUT_SECONDS must be positive")
        if self.delivery_poll_interval_seconds <= 0:
            raise ValueError("DELIVERY_POLL_INTERVAL_SECONDS must be positive")
        if self.telegram_min_request_interval_seconds <= 0:
            raise ValueError("TELEGRAM_MIN_REQUEST_INTERVAL_SECONDS must be positive")
        if self.telegram_inbound_rate_limit_per_minute <= 0:
            raise ValueError("TELEGRAM_INBOUND_RATE_LIMIT_PER_MINUTE must be positive")
        if self.telegram_inbound_rate_limit_per_hour < self.telegram_inbound_rate_limit_per_minute:
            raise ValueError(
                "TELEGRAM_INBOUND_RATE_LIMIT_PER_HOUR must be greater than or equal to "
                "TELEGRAM_INBOUND_RATE_LIMIT_PER_MINUTE"
            )
        if self.delivery_max_attempts <= 0:
            raise ValueError("DELIVERY_MAX_ATTEMPTS must be positive")
        if self.api_rate_limit_requests <= 0 or self.api_rate_limit_window_seconds <= 0:
            raise ValueError("API rate limit settings must be positive")
        if self.api_auth_failure_limit <= 0 or self.api_auth_failure_window_seconds <= 0:
            raise ValueError("API auth failure limit settings must be positive")
        if self.remnawave_reconcile_delay_seconds < 0:
            raise ValueError("REMNAWAVE_RECONCILE_DELAY_SECONDS must not be negative")
        if self.notification_webhook_enabled and (
            not self.notification_webhook_url or not self.notification_webhook_secret
        ):
            raise ValueError(
                "NOTIFICATION_WEBHOOK_URL and NOTIFICATION_WEBHOOK_SECRET are required "
                "when notification webhook is enabled"
            )
        if self.notification_webhook_enabled:
            assert self.notification_webhook_secret is not None
            validate_enabled_secret("NOTIFICATION_WEBHOOK_SECRET", self.notification_webhook_secret)
        if self.notification_webhook_timeout_seconds <= 0:
            raise ValueError("NOTIFICATION_WEBHOOK_TIMEOUT_SECONDS must be positive")
        if self.notification_webhook_max_attempts <= 0:
            raise ValueError("NOTIFICATION_WEBHOOK_MAX_ATTEMPTS must be positive")
        if self.notification_webhook_poll_interval_seconds <= 0:
            raise ValueError("NOTIFICATION_WEBHOOK_POLL_INTERVAL_SECONDS must be positive")
        return self


@lru_cache
def get_settings() -> Settings:
    # Settings reads required values from the environment at runtime.
    return Settings()  # type: ignore[call-arg]
