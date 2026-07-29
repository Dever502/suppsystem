from __future__ import annotations

from supportbot.config import Settings


class AuthorizationService:
    """Application-level admins; Telegram group permissions are not authorization."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_admin(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.settings.admin_telegram_ids
