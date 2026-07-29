from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from supportbot.service_types import TicketView
from supportbot.telegram_formatting import customer_identity

RATING_CALLBACK_PREFIX = "support_rating"


def message_command(message: Message) -> str:
    text = message.text or message.caption or ""
    return text.split(maxsplit=1)[0].lower().split("@", maxsplit=1)[0]


def command_argument(message: Message) -> str:
    text = message.text or message.caption or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


def message_text(message: Message) -> str | None:
    return message.text or message.caption


def metadata_fields(source: object, fields: tuple[str, ...]) -> dict[str, object]:
    return {field: value for field in fields if (value := getattr(source, field, None)) is not None}


def attachment_metadata(message: Message, content_type: str) -> dict[str, object]:
    fields = (
        "file_id",
        "file_unique_id",
        "file_size",
        "file_name",
        "mime_type",
        "width",
        "height",
        "duration",
        "performer",
        "title",
        "emoji",
        "set_name",
        "is_animated",
        "is_video",
    )
    if content_type == "photo":
        photos = message.photo or []
        if not photos:
            return {}
        metadata = metadata_fields(photos[-1], fields)
        metadata["photo_size_count"] = len(photos)
        return metadata
    attachment = getattr(message, content_type, None)
    return metadata_fields(attachment, fields) if attachment is not None else {}


def media_metadata(message: Message) -> dict[str, object] | None:
    content_type = getattr(message.content_type, "value", str(message.content_type))
    if content_type == "text":
        return None
    metadata = {"telegram_content_type": content_type}
    metadata.update(attachment_metadata(message, content_type))
    return metadata


def rating_keyboard(ticket_id: str, close_cycle: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ {score}",
                    callback_data=f"{RATING_CALLBACK_PREFIX}:{ticket_id}:{close_cycle}:{score}",
                )
                for score in range(1, 6)
            ]
        ]
    )


def rating_report(ticket: TicketView, score: int) -> str:
    return (
        "⭐ <b>Оценка поддержки</b>\n\n"
        f"Оценка: {'⭐' * score} <b>{score}/5</b>\n\n"
        "👤 <b>Клиент</b>\n\n"
        f"<b>{customer_identity(ticket)}</b>\n"
        f"Telegram ID: <code>{ticket.telegram_user_id}</code>"
    )


def command_key(message: Message, command: str) -> str:
    return f"telegram:{message.chat.id}:{message.message_id}:{command}"
