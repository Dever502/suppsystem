from __future__ import annotations

import enum

from supportbot.config import Settings


class OperatorRole(enum.StrEnum):
    FULL_ADMIN = "full_admin"
    OPERATOR = "operator"
    OPERATOR_RO = "operator_ro"


class AuthorizationService:
    """Application-level roles; Telegram group permissions are not authorization."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def role_for(self, telegram_user_id: int) -> OperatorRole | None:
        if telegram_user_id in self.settings.effective_full_admin_ids:
            return OperatorRole.FULL_ADMIN
        if telegram_user_id in self.settings.operator_telegram_ids:
            return OperatorRole.OPERATOR
        if telegram_user_id in self.settings.readonly_operator_telegram_ids:
            return OperatorRole.OPERATOR_RO
        return None

    def can_read(self, telegram_user_id: int) -> bool:
        return self.role_for(telegram_user_id) is not None

    def can_reply(self, telegram_user_id: int) -> bool:
        return self.role_for(telegram_user_id) in {
            OperatorRole.FULL_ADMIN,
            OperatorRole.OPERATOR,
        }

    def has_full_access(self, telegram_user_id: int) -> bool:
        return self.can_reply(telegram_user_id)

    def can_execute_topic_action(self, telegram_user_id: int, command: str) -> bool:
        role = self.role_for(telegram_user_id)
        if role in {OperatorRole.FULL_ADMIN, OperatorRole.OPERATOR}:
            return True
        return role is OperatorRole.OPERATOR_RO and command in {"/info", "/subinfo"}
