from __future__ import annotations

from datetime import datetime
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


def gift_notification_text(extend_days: int, expire_at: datetime) -> str:
    remainder_100 = extend_days % 100
    remainder_10 = extend_days % 10
    if remainder_10 == 1 and remainder_100 != 11:
        days_text = f"{extend_days} день"
        verb = "добавлен"
    elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        days_text = f"{extend_days} дня"
        verb = "добавлено"
    else:
        days_text = f"{extend_days} дней"
        verb = "добавлено"
    expiration = f"{expire_at.day} {RUSSIAN_MONTHS[expire_at.month]} {expire_at.year}"
    return (
        "🎁 <b>Подписка продлена</b>\n\n"
        f"Вам {verb} <b>{days_text}</b> подписки.\n\n"
        f"Новая дата окончания: <b>{expiration}</b>"
    )


def revoke_link_notification_text(subscription_url: str) -> str:
    safe_url = escape(subscription_url)
    return (
        "🔐 <b>Ссылка подписки обновлена</b>\n\n"
        "Старая ссылка больше не работает.\n\n"
        "Новая ссылка:\n"
        f"<code>{safe_url}</code>"
    )
