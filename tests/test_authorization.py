from __future__ import annotations

from pydantic import SecretStr

from supportbot.authorization import AuthorizationService
from supportbot.config import Settings


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "support_bot_token": SecretStr("test-token"),
        "support_group_id": -100123,
        "admin_telegram_ids": {1, 2},
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_only_configured_admins_are_authorized() -> None:
    authorization = AuthorizationService(settings())

    assert authorization.is_admin(1) is True
    assert authorization.is_admin(2) is True
    assert authorization.is_admin(3) is False
