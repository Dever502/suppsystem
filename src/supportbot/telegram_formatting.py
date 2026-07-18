from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from supportbot.models import TicketStatus
from supportbot.panel import (
    PanelActionResult,
    PanelActionStatus,
    PanelLookupStatus,
    PanelSubscriptionLookup,
)
from supportbot.service_types import TicketView

COMMAND_ALREADY_HANDLED_TEXT = "ℹ️ Команда уже обработана."


def panel_status_text(status: PanelLookupStatus | PanelActionStatus) -> str:
    messages: dict[str, str] = {
        "found": "найдено",
        "not_found": "пользователь не найден",
        "ambiguous_identity": (
            "найдено несколько пользователей с этим Telegram ID; операция заблокирована"
        ),
        "auth_error": "ошибка авторизации Remnawave",
        "rate_limited": "Remnawave вернул rate limit",
        "validation_error": "Remnawave отклонил параметры запроса",
        "unavailable": "Remnawave недоступен",
        "not_applied": "изменение не подтверждено; команду можно повторить",
        "unknown": "результат операции неизвестен; нужна сверка перед повтором",
        "needs_reconcile": "предыдущая операция ожидает сверки; повтор заблокирован",
        "unexpected_response": "неожиданный ответ Remnawave",
    }
    return messages[status]


def panel_action_reply(result: PanelActionResult) -> str:
    if result.status == "duplicate":
        return COMMAND_ALREADY_HANDLED_TEXT
    if result.status == "unknown" and result.action in {
        "extend_subscription",
        "revoke_subscription_link",
    }:
        return (
            "⏳ <b>Remnawave</b>\n\n"
            "Результат операции уточняется автоматически. Повтор пока заблокирован."
        )
    if result.status != "completed":
        return "⚠️ <b>Remnawave</b>\n\n" + panel_status_text(result.status)
    if result.action == "extend_subscription":
        suffix = (
            f"\nЗатронуто записей: <b>{result.affected_rows}</b>."
            if result.affected_rows is not None
            else ""
        )
        return "✅ <b>Подписка продлена</b>" + suffix
    if result.action == "reset_key":
        return "✅ Ключи подписки перевыпущены."
    if result.action == "revoke_subscription_link":
        return "✅ Ссылка подписки перевыпущена."
    if result.action == "reset_devices":
        suffix = (
            f"\nУдалено устройств: <b>{result.devices_removed}</b>."
            if result.devices_removed is not None
            else ""
        )
        return "✅ <b>Устройства сброшены</b>" + suffix
    return "✅ Операция Remnawave выполнена."


def ticket_status_text(status: TicketStatus) -> str:
    return {
        TicketStatus.OPEN: "🟢 <code>Открыт</code>",
        TicketStatus.CLOSED: "⚪ <code>Закрыт</code>",
        TicketStatus.PROVISIONING: "🟡 <code>Создание темы</code>",
    }[status]


def date_text(value: object | None) -> str:
    if not isinstance(value, datetime):
        return "—"
    return value.strftime("%d.%m.%Y, %H:%M %Z").rstrip()


def customer_identity(ticket: TicketView) -> str:
    parts = []
    if ticket.display_name:
        parts.append(escape(ticket.display_name))
    if ticket.username:
        parts.append(f"@{escape(ticket.username)}")
    return " · ".join(parts) or "Без имени"


def operator_ticket_info(ticket: TicketView) -> str:
    topic_id = ticket.topic_id if ticket.topic_id is not None else "—"
    return (
        "🎫 <b>Тикет</b>\n\n"
        f"Статус: {ticket_status_text(ticket.status)}\n"
        f"ID: <code>{escape(ticket.id)}</code>\n"
        f"Тема: <code>{topic_id}</code>\n\n"
        "👤 <b>Клиент</b>\n\n"
        f"<b>{customer_identity(ticket)}</b>\n"
        f"Telegram ID: <code>{ticket.telegram_user_id}</code>\n\n"
        "🕒 <b>История</b>\n\n"
        f"Создан: <code>{date_text(ticket.created_at)}</code>\n"
        f"Обновлён: <code>{date_text(ticket.updated_at)}</code>\n"
        f"Закрыт: <code>{date_text(ticket.closed_at)}</code>"
    )


def topic_identity(ticket: TicketView) -> str:
    if ticket.display_name:
        return ticket.display_name
    if ticket.username:
        return f"@{ticket.username}"
    return f"tg:{ticket.telegram_user_id}"


def topic_name(ticket: TicketView, *, closed: bool) -> str:
    marker = "🟢" if closed else "🔴"
    identity = topic_identity(ticket)
    if ticket.display_name or ticket.username:
        identity = f"{identity} · tg:{str(ticket.telegram_user_id)[-4:]}"
    return f"{marker} {identity}"[:128]


def subscription_status(status: str) -> str:
    markers = {"ACTIVE": "🟢", "LIMITED": "🟡", "DISABLED": "🔴", "EXPIRED": "🔴"}
    return f"{markers.get(status.upper(), '⚪')} <code>{escape(status)}</code>"


def expiration_text(expire_at: datetime, *, now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if expire_at.tzinfo is not None:
        current = current.astimezone(expire_at.tzinfo)
    days = (expire_at.date() - current.date()).days
    if days < 0:
        suffix = "истекла"
    elif days == 0:
        suffix = "сегодня"
    else:
        remainder_100 = days % 100
        remainder_10 = days % 10
        if remainder_10 == 1 and remainder_100 != 11:
            unit = "день"
        elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
            unit = "дня"
        else:
            unit = "дней"
        suffix = f"{days} {unit}"
    return f"{expire_at:%d.%m.%Y} ({suffix})"


def code_or_dash(value: object | None) -> str:
    return "—" if value is None else f"<code>{escape(str(value))}</code>"


def traffic_text(value: int | None) -> str:
    if value is None:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.2f} {unit}"


def format_subscription_lookup(lookup: PanelSubscriptionLookup) -> str:
    identity = f"{escape(lookup.identity_provider)}:<code>{escape(lookup.identity_value)}</code>"
    if lookup.subscription is None:
        return (
            "💳 <b>Подписка Remnawave</b>\n\n"
            f"Поиск: {identity}\n"
            f"Статус: {panel_status_text(lookup.status)}"
        )
    subscription = lookup.subscription
    return (
        "💳 <b>Подписка Remnawave</b>\n\n"
        f"Пользователь: <code>{escape(subscription.username)}</code>\n"
        f"Статус: {subscription_status(subscription.status)}\n"
        f"Действует до: <code>{expiration_text(subscription.expire_at)}</code>\n"
        f"Email: {code_or_dash(subscription.email)}\n"
        f"Telegram ID: {code_or_dash(subscription.telegram_id)}\n"
        f"Лимит устройств: {code_or_dash(subscription.hwid_device_limit)}\n\n"
        "📊 <b>Активность</b>\n\n"
        f"Трафик: <code>{traffic_text(subscription.used_traffic_bytes)}</code>\n"
        f"За всё время: <code>{traffic_text(subscription.lifetime_used_traffic_bytes)}</code>\n"
        f"Последний онлайн: <code>{date_text(subscription.online_at)}</code>\n\n"
        "🔗 <b>Ссылка подписки</b>\n\n"
        f"<code>{escape(subscription.subscription_url)}</code>"
    )
