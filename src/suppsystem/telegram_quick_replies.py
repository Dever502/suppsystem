from __future__ import annotations

import logging
import math
from datetime import UTC

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.quick_replies import (
    QUICK_REPLY_TEXT_MAX_LENGTH,
    QUICK_REPLY_TITLE_MAX_LENGTH,
    QuickReplyService,
    QuickReplyTitleConflictError,
    QuickReplyView,
    utf16_code_units,
)
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.telegram_message_utils import command_argument

logger = logging.getLogger(__name__)

QUICK_REPLY_CALLBACK_PREFIX = "suppsystem_answers"
QUICK_REPLY_PAGE_SIZE = 8
TELEGRAM_COPY_TEXT_LIMIT = 256
ADD_ANSWER_COMMAND = "/addanswer"
ANSWERS_COMMAND = "/answers"


def parse_add_answer_argument(argument: str) -> tuple[str, str] | None:
    title, separator, text = argument.partition("\n")
    clean_title = " ".join(title.split())
    clean_text = text.strip()
    if (
        not separator
        or not clean_title
        or not clean_text
        or utf16_code_units(clean_title) > QUICK_REPLY_TITLE_MAX_LENGTH
        or utf16_code_units(clean_text) > QUICK_REPLY_TEXT_MAX_LENGTH
    ):
        return None
    return clean_title, clean_text


def _truncate_utf16(value: str, limit: int) -> str:
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


def quick_reply_card(reply: QuickReplyView) -> str:
    created_at = reply.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    from suppsystem.statistics import MOSCOW

    operator = _truncate_utf16(
        reply.created_by_display_name
        or (f"@{reply.created_by_username}" if reply.created_by_username else None)
        or f"TG:{reply.created_by_telegram_id}",
        64,
    )
    return (
        f"💬 Готовый ответ #{reply.id}\n\n"
        f"{reply.title}\n\n"
        f"{reply.text}\n\n"
        f"Добавил: {operator} · {created_at.astimezone(MOSCOW):%d.%m.%Y, %H:%M} MSK"
    )


def _callback(action: str, owner_id: int, *values: int) -> str:
    suffix = ":".join(str(value) for value in values)
    return f"{QUICK_REPLY_CALLBACK_PREFIX}:{action}:{owner_id}" + (f":{suffix}" if suffix else "")


class TelegramQuickReplyHandlers:
    bot: Bot
    authorization: AuthorizationService
    limiter: TelegramRateLimiter
    settings: Settings
    quick_reply_service: QuickReplyService | None
    quick_replies_topic_id: int | None

    async def _delete_quick_reply_message(self, message: Message) -> None:
        try:
            await self.limiter.wait()
            await self.bot.delete_message(
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except TelegramAPIError:
            logger.warning(
                "Unable to delete quick reply command or panel",
                exc_info=True,
                extra={
                    "event": "quick_reply_message_delete_failed",
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                },
            )

    async def _quick_reply_list(
        self, owner_id: int, requested_page: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        assert self.quick_reply_service is not None
        page = max(0, requested_page)
        replies, total = await self.quick_reply_service.list_active(
            offset=page * QUICK_REPLY_PAGE_SIZE,
            limit=QUICK_REPLY_PAGE_SIZE,
        )
        pages = max(1, math.ceil(total / QUICK_REPLY_PAGE_SIZE))
        if page >= pages:
            page = pages - 1
            replies, total = await self.quick_reply_service.list_active(
                offset=page * QUICK_REPLY_PAGE_SIZE,
                limit=QUICK_REPLY_PAGE_SIZE,
            )

        rows = [
            [
                InlineKeyboardButton(
                    text=reply.title,
                    callback_data=_callback("view", owner_id, reply.id, page),
                )
            ]
            for reply in replies
        ]
        if pages > 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=_callback("list", owner_id, max(0, page - 1)),
                    ),
                    InlineKeyboardButton(
                        text=f"{page + 1}/{pages}",
                        callback_data=_callback("list", owner_id, page),
                    ),
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=_callback("list", owner_id, min(pages - 1, page + 1)),
                    ),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data=_callback("close", owner_id),
                )
            ]
        )
        text = (
            "📚 Готовые ответы\n\nВыберите нужный ответ:"
            if total
            else "📚 Готовые ответы\n\nСписок пока пуст. Добавьте первый ответ командой /addanswer."
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _quick_reply_preview(
        self, owner_id: int, reply: QuickReplyView, page: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        rows: list[list[InlineKeyboardButton]] = []
        if utf16_code_units(reply.text) <= TELEGRAM_COPY_TEXT_LIMIT:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📋 Скопировать",
                        copy_text=CopyTextButton(text=reply.text),
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📄 Показать чистый текст",
                        callback_data=_callback("text", owner_id, reply.id),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ К списку",
                    callback_data=_callback("list", owner_id, page),
                ),
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data=_callback("close", owner_id),
                ),
            ]
        )
        return (
            f"📝 {reply.title}\n\n{reply.text}",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _handle_add_answer(self, message: Message) -> None:
        assert message.from_user is not None
        assert self.quick_reply_service is not None
        parsed = parse_add_answer_argument(command_argument(message))
        if parsed is None:
            await message.reply(
                "⚠️ Формат:\n"
                "/addanswer Название кнопки\n"
                "Текст готового ответа\n\n"
                f"Название — до {QUICK_REPLY_TITLE_MAX_LENGTH} символов, "
                f"текст — до {QUICK_REPLY_TEXT_MAX_LENGTH}."
            )
            return
        title, text = parsed
        try:
            result = await self.quick_reply_service.create(
                title=title,
                text=text,
                operator_telegram_id=message.from_user.id,
                operator_display_name=message.from_user.full_name or None,
                operator_username=message.from_user.username,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
            )
        except QuickReplyTitleConflictError:
            await message.reply("⚠️ Готовый ответ с таким названием уже существует.")
            return

        reply = result.reply
        if reply.published_message_id is None:
            await self.limiter.wait()
            published = await self.bot.send_message(
                chat_id=message.chat.id,
                message_thread_id=message.message_thread_id,
                text=quick_reply_card(reply),
                parse_mode=None,
            )
            if not await self.quick_reply_service.mark_published(reply.id, published.message_id):
                await self._delete_quick_reply_message(published)
        await self._delete_quick_reply_message(message)

    async def _handle_answers(self, message: Message) -> None:
        assert message.from_user is not None
        text, keyboard = await self._quick_reply_list(message.from_user.id, 0)
        await self.limiter.wait()
        await self.bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=None,
        )
        await self._delete_quick_reply_message(message)

    async def handle_quick_reply_topic_message(self, message: Message, command: str) -> bool:
        topic_id = getattr(self, "quick_replies_topic_id", None)
        service = getattr(self, "quick_reply_service", None)
        if topic_id is None or message.message_thread_id != topic_id:
            return False
        if service is None:
            await message.reply("⚠️ Готовые ответы временно недоступны.")
            return True
        if command == ADD_ANSWER_COMMAND:
            await self._handle_add_answer(message)
        elif command == ANSWERS_COMMAND:
            await self._handle_answers(message)
        elif command.startswith("/"):
            await message.reply("❓ В этом топике доступны команды /addanswer и /answers.")
        return True

    async def handle_quick_reply_callback(self, callback: CallbackQuery) -> None:
        if callback.from_user is None or not self.authorization.is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        service = getattr(self, "quick_reply_service", None)
        topic_id = getattr(self, "quick_replies_topic_id", None)
        if service is None or topic_id is None:
            await callback.answer("Готовые ответы недоступны.", show_alert=True)
            return

        parts = (callback.data or "").split(":")
        try:
            action = parts[1]
            owner_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некорректная кнопка.", show_alert=False)
            return
        if owner_id != callback.from_user.id:
            await callback.answer("Эта панель открыта другим оператором.", show_alert=True)
            return

        message = callback.message
        if (
            not isinstance(message, Message)
            or message.message_thread_id != topic_id
            or message.chat.id != self.settings.support_group_id
        ):
            await callback.answer("Панель больше недоступна.", show_alert=False)
            return

        if action == "close" or action == "delete":
            await self._delete_quick_reply_message(message)
            await callback.answer()
            return
        if action == "list":
            try:
                page = int(parts[3])
            except (IndexError, ValueError):
                page = 0
            text, keyboard = await self._quick_reply_list(owner_id, page)
            await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            await callback.answer()
            return
        if action == "view":
            try:
                reply_id = int(parts[3])
                page = int(parts[4])
            except (IndexError, ValueError):
                await callback.answer("Некорректная кнопка.", show_alert=False)
                return
            reply = await service.get_active(reply_id)
            if reply is None:
                await callback.answer("Ответ не найден.", show_alert=True)
                return
            text, keyboard = await self._quick_reply_preview(owner_id, reply, page)
            await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            await callback.answer()
            return
        if action == "text":
            try:
                reply_id = int(parts[3])
            except (IndexError, ValueError):
                await callback.answer("Некорректная кнопка.", show_alert=False)
                return
            reply = await service.get_active(reply_id)
            if reply is None:
                await callback.answer("Ответ не найден.", show_alert=True)
                return
            await self.limiter.wait()
            await self.bot.send_message(
                chat_id=self.settings.support_group_id,
                message_thread_id=topic_id,
                text=reply.text,
                parse_mode=None,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🗑 Удалить сообщение",
                                callback_data=_callback("delete", owner_id),
                            )
                        ]
                    ]
                ),
            )
            await callback.answer("Текст отправлен отдельным сообщением.", show_alert=False)
            return
        await callback.answer("Некорректная кнопка.", show_alert=False)
