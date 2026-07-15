from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from supportbot.authorization import AuthorizationService, OperatorRole
from supportbot.config import Settings


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "support_bot_token": SecretStr("test-token"),
        "support_group_id": -100123,
        "full_admin_telegram_ids": {1},
        "operator_telegram_ids": {2},
        "readonly_operator_telegram_ids": {3},
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_roles_have_expected_permissions() -> None:
    authorization = AuthorizationService(settings())

    assert authorization.role_for(1) is OperatorRole.FULL_ADMIN
    assert authorization.role_for(2) is OperatorRole.OPERATOR
    assert authorization.role_for(3) is OperatorRole.OPERATOR_RO
    assert authorization.role_for(4) is None

    for telegram_id in (1, 2):
        assert authorization.can_read(telegram_id) is True
        assert authorization.can_reply(telegram_id) is True
        assert authorization.can_execute_topic_action(telegram_id, "/gift") is True
        assert authorization.has_full_access(telegram_id) is True

    assert authorization.can_read(3) is True
    assert authorization.can_reply(3) is False
    assert authorization.can_execute_topic_action(3, "/info") is True
    assert authorization.can_execute_topic_action(3, "/subinfo") is True
    assert authorization.can_execute_topic_action(3, "/gift") is False
    assert authorization.can_execute_topic_action(3, "") is False


def test_legacy_admin_ids_are_full_admins() -> None:
    authorization = AuthorizationService(
        settings(full_admin_telegram_ids=set(), admin_telegram_ids={10})
    )

    assert authorization.role_for(10) is OperatorRole.FULL_ADMIN
    assert authorization.has_full_access(10) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_admin_telegram_ids": {1}, "operator_telegram_ids": {1}},
        {"full_admin_telegram_ids": {1}, "readonly_operator_telegram_ids": {1}},
        {"operator_telegram_ids": {2}, "readonly_operator_telegram_ids": {2}},
        {"admin_telegram_ids": {2}, "operator_telegram_ids": {2}},
    ],
)
def test_role_lists_must_not_overlap(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        settings(**overrides)
