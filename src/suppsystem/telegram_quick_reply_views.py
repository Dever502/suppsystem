from __future__ import annotations

from datetime import UTC, datetime

from aiogram.exceptions import TelegramBadRequest

from suppsystem.quick_replies import (
    QUICK_REPLY_GROUP_NAME_MAX_LENGTH,
    QUICK_REPLY_TEXT_MAX_LENGTH,
    QUICK_REPLY_TITLE_MAX_LENGTH,
    QuickReplyGroupView,
    QuickReplyView,
    utf16_code_units,
)

QUICK_REPLY_CALLBACK_PREFIX = "suppsystem_answers"
QUICK_REPLY_PAGE_SIZE = 8
TELEGRAM_COPY_TEXT_LIMIT = 256
TELEGRAM_BUTTON_TEXT_LIMIT = 64
QUICK_REPLY_INLINE_PAGE_SIZE = 20
QUICK_REPLY_INLINE_QUERY_PREFIX = "qr-group-"
QUICK_REPLY_DRAFT_TIMEOUT_SECONDS = 600.0
ADD_GROUP_COMMAND = "/addgroup"
ADD_ANSWER_COMMAND = "/addanswer"
ANSWERS_COMMAND = "/answers"


def parse_add_group_argument(argument: str) -> str | None:
    clean_name = " ".join(argument.split())
    if (
        not clean_name
        or "\n" in argument
        or "|" in clean_name
        or utf16_code_units(clean_name) > QUICK_REPLY_GROUP_NAME_MAX_LENGTH
    ):
        return None
    return clean_name


def parse_add_answer_argument(argument: str) -> tuple[str, str, str] | None:
    heading, line_separator, text = argument.partition("\n")
    group_name, group_separator, title = heading.partition("|")
    clean_group_name = " ".join(group_name.split())
    clean_title = " ".join(title.split())
    clean_text = text.strip()
    if (
        not line_separator
        or not group_separator
        or not clean_group_name
        or not clean_title
        or not clean_text
        or utf16_code_units(clean_group_name) > QUICK_REPLY_GROUP_NAME_MAX_LENGTH
        or utf16_code_units(clean_title) > QUICK_REPLY_TITLE_MAX_LENGTH
        or utf16_code_units(clean_text) > QUICK_REPLY_TEXT_MAX_LENGTH
    ):
        return None
    return clean_group_name, clean_title, clean_text


def clean_draft_title(value: str) -> str | None:
    clean_title = " ".join(value.split())
    if not clean_title or utf16_code_units(clean_title) > QUICK_REPLY_TITLE_MAX_LENGTH:
        return None
    return clean_title


def clean_draft_text(value: str) -> str | None:
    clean_text = value.strip()
    if not clean_text or utf16_code_units(clean_text) > QUICK_REPLY_TEXT_MAX_LENGTH:
        return None
    return clean_text


def truncate_utf16(value: str, limit: int) -> str:
    if utf16_code_units(value) <= limit:
        return value
    result: list[str] = []
    used = 0
    for character in value:
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > limit - 1:
            break
        result.append(character)
        used += width
    return "".join(result) + "…"


def _operator_label(
    *,
    telegram_id: int,
    display_name: str | None,
    username: str | None,
) -> str:
    return truncate_utf16(
        display_name or (f"@{username}" if username else None) or f"TG:{telegram_id}",
        64,
    )


def _created_at_text(created_at: datetime) -> str:
    from suppsystem.statistics import MOSCOW

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return f"{created_at.astimezone(MOSCOW):%d.%m.%Y, %H:%M} MSK"


def quick_reply_group_card(group: QuickReplyGroupView) -> str:
    operator = _operator_label(
        telegram_id=group.created_by_telegram_id,
        display_name=group.created_by_display_name,
        username=group.created_by_username,
    )
    return (
        f"📁 Группа готовых ответов #{group.id}\n\n"
        f"{group.name}\n\n"
        f"Добавил: {operator} · {_created_at_text(group.created_at)}"
    )


def quick_reply_card(reply: QuickReplyView, group_name: str) -> str:
    operator = _operator_label(
        telegram_id=reply.created_by_telegram_id,
        display_name=reply.created_by_display_name,
        username=reply.created_by_username,
    )
    return (
        f"💬 Готовый ответ #{reply.id}\n\n"
        f"📁 {group_name}\n"
        f"{reply.title}\n\n"
        f"{reply.text}\n\n"
        f"Добавил: {operator} · {_created_at_text(reply.created_at)}"
    )


def callback_data(action: str, owner_id: int, *values: int) -> str:
    suffix = ":".join(str(value) for value in values)
    return f"{QUICK_REPLY_CALLBACK_PREFIX}:{action}:{owner_id}" + (f":{suffix}" if suffix else "")


def shared_callback_data(action: str, *values: int) -> str:
    suffix = ":".join(str(value) for value in values)
    return f"{QUICK_REPLY_CALLBACK_PREFIX}:{action}" + (f":{suffix}" if suffix else "")


def inline_group_query(group_id: int) -> str:
    return f"{QUICK_REPLY_INLINE_QUERY_PREFIX}{group_id}"


def parse_inline_group_query(query: str) -> int | None:
    token = query.strip().split(maxsplit=1)[0] if query.strip() else ""
    if not token.startswith(QUICK_REPLY_INLINE_QUERY_PREFIX):
        return None
    try:
        group_id = int(token.removeprefix(QUICK_REPLY_INLINE_QUERY_PREFIX))
    except ValueError:
        return None
    return group_id if group_id > 0 else None


def inline_reply_description(text: str, *, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def message_missing(error: TelegramBadRequest) -> bool:
    message = str(error).casefold()
    return any(
        fragment in message
        for fragment in (
            "message to edit not found",
            "message can't be edited",
            "message_id_invalid",
        )
    )


def message_not_modified(error: TelegramBadRequest) -> bool:
    return "message is not modified" in str(error).casefold()
