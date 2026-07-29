from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from supportbot.config import Settings

STRONG_SECRET = "0123456789abcdef0123456789abcdef"


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "support_bot_token": SecretStr("test-token"),
        "support_group_id": -100123,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_data_dir_controls_default_sqlite_database_url(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    configured = settings(data_dir=data_dir)

    assert configured.database_url == f"sqlite+aiosqlite:///{data_dir / 'support.db'}"


def test_database_url_override_wins_over_data_dir(tmp_path: Path) -> None:
    configured = settings(
        data_dir=tmp_path / "runtime",
        database_url="postgresql+asyncpg://user:0123456789abcdef@postgres:5432/support",
    )

    assert (
        configured.database_url
        == "postgresql+asyncpg://user:0123456789abcdef@postgres:5432/support"
    )


def test_migration_database_url_defaults_to_runtime_database_url(tmp_path: Path) -> None:
    configured = settings(data_dir=tmp_path)

    assert configured.migration_database_url == configured.database_url


def test_migration_database_url_can_use_a_separate_role(tmp_path: Path) -> None:
    runtime_url = "postgresql+asyncpg://runtime:runtime-password-123@db/supportbot"
    migration_url = "postgresql+asyncpg://migrator:migration-password-123@db/supportbot"

    configured = settings(
        data_dir=tmp_path,
        database_url=runtime_url,
        migration_database_url=migration_url,
    )

    assert configured.database_url == runtime_url
    assert configured.migration_database_url == migration_url


def test_startup_migrations_are_enabled_by_default_and_can_be_delegated() -> None:
    assert settings().migrations_at_startup is True
    assert settings(migrations_at_startup=False).migrations_at_startup is False


def test_revoke_link_telegram_notification_is_enabled_by_default_and_can_be_disabled() -> None:
    assert settings().remnawave_revoke_link_telegram_notification is True
    assert (
        settings(
            remnawave_revoke_link_telegram_notification=False
        ).remnawave_revoke_link_telegram_notification
        is False
    )


def test_inbound_telegram_rate_limits_are_gentle_by_default() -> None:
    configured = settings()

    assert configured.telegram_inbound_rate_limit_per_minute == 30
    assert configured.telegram_inbound_rate_limit_per_hour == 150


def test_admin_ids_accept_comma_separated_values() -> None:
    configured = settings(admin_telegram_ids="8387907909, 7")

    assert configured.admin_telegram_ids == frozenset({8387907909, 7})


def test_configuration_error_formatter_is_operator_readable() -> None:
    from supportbot.__main__ import format_configuration_error

    with pytest.raises(ValidationError) as captured:
        settings(delivery_poll_interval_seconds=0)

    message = format_configuration_error(captured.value)

    assert message.startswith("Configuration error:\n")
    assert "DELIVERY_POLL_INTERVAL_SECONDS" in message
    assert "/opt/supportbot/.env" in message
    assert "Traceback" not in message
    assert "pydantic_core" not in message
    assert "errors.pydantic.dev" not in message


def test_enabled_integrations_accept_https_and_strong_secrets() -> None:
    configured = settings(
        api_enabled=True,
        api_admin_token=SecretStr(STRONG_SECRET),
        remnawave_enabled=True,
        remnawave_base_url="https://panel.example.com/api",
        remnawave_api_token=SecretStr(STRONG_SECRET),
        notification_webhook_enabled=True,
        notification_webhook_url="https://receiver.example.com/support?version=1",
        notification_webhook_secret=SecretStr(STRONG_SECRET),
    )

    assert configured.remnawave_enabled is True
    assert configured.notification_webhook_enabled is True


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost:3000",
        "http://127.42.0.1:3000",
        "http://[::1]:3000",
    ),
)
def test_loopback_http_is_allowed_for_development(url: str) -> None:
    assert settings(remnawave_base_url=url).remnawave_base_url == url


@pytest.mark.parametrize(
    "url",
    (
        "http://panel.example.com",
        "http://10.0.0.10",
        "http://192.168.1.10",
        "ftp://panel.example.com",
        "https:///missing-host",
        "https://bad_host.example.com",
    ),
)
def test_unsafe_or_malformed_external_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="REMNAWAVE_BASE_URL"):
        settings(remnawave_base_url=url)


@pytest.mark.parametrize(
    "url",
    (
        "https://private-user:private-password@panel.example.com",
        "https://panel.example.com/path#private-fragment",
    ),
)
def test_url_validation_errors_redact_the_input(url: str) -> None:
    with pytest.raises(ValidationError) as captured:
        settings(notification_webhook_url=url)

    message = str(captured.value)
    assert "NOTIFICATION_WEBHOOK_URL" in message
    assert url not in message
    assert "private-password" not in message
    assert "private-fragment" not in message


def test_disabled_integrations_do_not_require_urls_or_secrets() -> None:
    configured = settings()

    assert configured.api_admin_token is None
    assert configured.remnawave_base_url is None
    assert configured.notification_webhook_url is None


def test_empty_optional_environment_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMNAWAVE_BASE_URL", "")
    monkeypatch.setenv("REMNAWAVE_API_TOKEN", "")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_SECRET", "")

    configured = settings()

    assert configured.remnawave_base_url is None
    assert configured.remnawave_api_token is None
    assert configured.notification_webhook_url is None
    assert configured.notification_webhook_secret is None


@pytest.mark.parametrize(
    "overrides",
    (
        {"api_enabled": True, "api_admin_token": SecretStr("short-api-token")},
        {
            "remnawave_enabled": True,
            "remnawave_base_url": "https://panel.example.com",
            "remnawave_api_token": SecretStr("short-panel-token"),
        },
        {
            "notification_webhook_enabled": True,
            "notification_webhook_url": "https://receiver.example.com",
            "notification_webhook_secret": SecretStr("short-webhook-secret"),
        },
    ),
)
def test_enabled_integrations_reject_short_secrets(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        settings(**overrides)


def test_placeholder_secret_is_rejected_even_when_long_enough() -> None:
    placeholder = "change-me-change-me-change-me-change-me"

    with pytest.raises(ValidationError) as captured:
        settings(api_enabled=True, api_admin_token=SecretStr(placeholder))

    assert placeholder not in str(captured.value)
    assert "API_ADMIN_TOKEN" in str(captured.value)


def test_secret_validation_error_does_not_contain_the_secret() -> None:
    sensitive_value = "sensitive-but-too-short"

    with pytest.raises(ValidationError) as captured:
        settings(
            notification_webhook_enabled=True,
            notification_webhook_url="https://receiver.example.com",
            notification_webhook_secret=SecretStr(sensitive_value),
        )

    assert sensitive_value not in str(captured.value)


@pytest.mark.parametrize(
    "overrides",
    (
        {"delivery_poll_interval_seconds": 0},
        {"telegram_min_request_interval_seconds": 0},
        {"telegram_inbound_rate_limit_per_minute": 0},
        {"telegram_inbound_rate_limit_per_hour": 0},
        {
            "telegram_inbound_rate_limit_per_minute": 31,
            "telegram_inbound_rate_limit_per_hour": 30,
        },
        {"delivery_max_attempts": 0},
    ),
)
def test_runtime_tuning_values_must_be_positive(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_trusted_proxy_ips_accept_cidr_and_reject_malformed_values() -> None:
    configured = settings(api_trusted_proxy_ips="127.0.0.1,10.0.0.0/24")

    assert configured.api_trusted_proxy_ips == frozenset({"127.0.0.1", "10.0.0.0/24"})
    with pytest.raises(ValidationError, match="API_TRUSTED_PROXY_IPS"):
        settings(api_trusted_proxy_ips="not-an-ip")


def test_postgres_database_url_rejects_placeholder_password() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        settings(database_url="postgresql+asyncpg://supportbot:supportbot@postgres:5432/supportbot")


def test_postgres_migration_database_url_rejects_placeholder_password() -> None:
    with pytest.raises(ValidationError, match="MIGRATION_DATABASE_URL"):
        settings(
            migration_database_url=(
                "postgresql+asyncpg://supportbot_migrator:supportbot@postgres:5432/supportbot"
            )
        )


def test_postgres_database_url_accepts_non_placeholder_password() -> None:
    configured = settings(
        database_url="postgresql+asyncpg://supportbot:0123456789abcdef@postgres:5432/supportbot"
    )

    assert configured.database_url == (
        "postgresql+asyncpg://supportbot:0123456789abcdef@postgres:5432/supportbot"
    )
