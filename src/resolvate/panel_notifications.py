from __future__ import annotations

from datetime import UTC, datetime
from html import escape

RUSSIAN_MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _day_unit(days: int) -> str:
    remainder_100 = days % 100
    remainder_10 = days % 10
    if remainder_10 == 1 and remainder_100 != 11:
        return "день"
    if remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        return "дня"
    return "дней"


def gift_notification_text(
    extend_days: int,
    expire_at: datetime,
    *,
    now: datetime | None = None,
) -> str:
    extension_unit = _day_unit(extend_days)
    days_text = f"{extend_days} {extension_unit}"
    verb = "добавлен" if extension_unit == "день" else "добавлено"
    current = now or datetime.now(UTC)
    if expire_at.tzinfo is not None:
        current = current.astimezone(expire_at.tzinfo)
    total_days = max(0, (expire_at.date() - current.date()).days)
    total_days_text = f"{total_days} {_day_unit(total_days)}"
    expiration = f"{expire_at.day} {RUSSIAN_MONTHS[expire_at.month]} {expire_at.year}"
    return (
        "🎁 <b>Подписка продлена</b>\n\n"
        f"Вам {verb} <b>{days_text}</b> подписки.\n\n"
        f"Новая дата окончания: <b>{expiration}</b> ({total_days_text})"
    )


def revoke_link_notification_text(subscription_url: str) -> str:
    safe_url = escape(subscription_url)
    return (
        "🔐 <b>Ссылка подписки обновлена</b>\n\n"
        "Старая ссылка больше не работает.\n\n"
        "Новая ссылка:\n"
        f"<code>{safe_url}</code>"
    )
