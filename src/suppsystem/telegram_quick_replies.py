from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultUnion,
    InputTextMessageContent,
    Message,
)

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.quick_replies import (
    QUICK_REPLY_GROUP_NAME_MAX_LENGTH,
    QUICK_REPLY_TEXT_MAX_LENGTH,
    QUICK_REPLY_TITLE_MAX_LENGTH,
    QuickReplyGroupNameConflictError,
    QuickReplyGroupView,
    QuickReplyService,
    QuickReplyTitleConflictError,
    QuickReplyView,
    utf16_code_units,
)
from suppsystem.telegram_errors import is_missing_topic_error
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.telegram_message_utils import command_argument
from suppsystem.telegram_quick_reply_drafts import TelegramQuickReplyDraftHandlers
from suppsystem.telegram_quick_reply_views import (
    ADD_ANSWER_COMMAND,
    ADD_GROUP_COMMAND,
    ANSWERS_COMMAND,
    QUICK_REPLY_INLINE_PAGE_SIZE,
    QUICK_REPLY_PAGE_SIZE,
    TELEGRAM_BUTTON_TEXT_LIMIT,
    TELEGRAM_COPY_TEXT_LIMIT,
    callback_data,
    inline_group_query,
    inline_reply_description,
    message_missing,
    message_not_modified,
    parse_add_answer_argument,
    parse_add_group_argument,
    parse_inline_group_query,
    shared_callback_data,
    truncate_utf16,
)
from suppsystem.telegram_quick_reply_views import (
    QUICK_REPLY_CALLBACK_PREFIX as QUICK_REPLY_CALLBACK_PREFIX,
)

logger = logging.getLogger(__name__)

QUICK_REPLY_MENU_REFRESH_INTERVAL_SECONDS = 60.0

_LEGACY_CATALOG_ACTIONS = {
    "menu_catalog",
    "menu_add",
    "groups",
    "group",
    "view",
    "addpicker",
    "draftselect",
    "draftnew",
    "draftback",
}


class TelegramQuickReplyHandlers(TelegramQuickReplyDraftHandlers):
    bot: Bot
    authorization: AuthorizationService
    limiter: TelegramRateLimiter
    settings: Settings
    quick_reply_service: QuickReplyService | None
    quick_replies_topic_id: int | None
    recover_quick_replies_topic: Callable[[int], Awaitable[int]] | None
    _quick_reply_menu_lock: asyncio.Lock

    async def _pin_quick_reply_menu(self, message_id: int) -> None:
        try:
            await self.limiter.wait()
            await self.bot.pin_chat_message(
                chat_id=self.settings.support_group_id,
                message_id=message_id,
                disable_notification=True,
            )
        except TelegramBadRequest as error:
            if "already pinned" in str(error).casefold():
                return
            logger.warning(
                "Unable to pin quick reply menu",
                exc_info=True,
                extra={
                    "event": "quick_reply_menu_pin_failed",
                    "message_id": message_id,
                },
            )
        except TelegramAPIError:
            logger.warning(
                "Unable to pin quick reply menu",
                exc_info=True,
                extra={
                    "event": "quick_reply_menu_pin_failed",
                    "message_id": message_id,
                },
            )

    @staticmethod
    def _group_button_text(group: QuickReplyGroupView) -> str:
        prefix = "📁 "
        suffix = f" · {group.reply_count}"
        text = f"{prefix}{group.name}{suffix}"
        if utf16_code_units(text) <= TELEGRAM_BUTTON_TEXT_LIMIT:
            return text
        name_limit = TELEGRAM_BUTTON_TEXT_LIMIT - utf16_code_units(prefix + suffix)
        return f"{prefix}{truncate_utf16(group.name, name_limit)}{suffix}"

    async def _quick_reply_shared_menu(
        self,
        requested_page: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        assert self.quick_reply_service is not None
        page = max(0, requested_page)
        groups, total = await self.quick_reply_service.list_groups(
            offset=page * QUICK_REPLY_PAGE_SIZE,
            limit=QUICK_REPLY_PAGE_SIZE,
        )
        pages = max(1, math.ceil(total / QUICK_REPLY_PAGE_SIZE))
        if page >= pages:
            page = pages - 1
            groups, total = await self.quick_reply_service.list_groups(
                offset=page * QUICK_REPLY_PAGE_SIZE,
                limit=QUICK_REPLY_PAGE_SIZE,
            )

        rows: list[list[InlineKeyboardButton]] = []
        for group in groups:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._group_button_text(group),
                        switch_inline_query_current_chat=inline_group_query(group.id),
                    ),
                    InlineKeyboardButton(
                        text="➕",
                        callback_data=shared_callback_data("add", group.id, page),
                    ),
                ]
            )
        if pages > 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=shared_callback_data(
                            "menupage",
                            max(0, page - 1),
                        ),
                    ),
                    InlineKeyboardButton(
                        text=f"{page + 1}/{pages}",
                        callback_data=shared_callback_data("menupage", page),
                    ),
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=shared_callback_data(
                            "menupage",
                            min(pages - 1, page + 1),
                        ),
                    ),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Создать новую группу",
                    callback_data=shared_callback_data("new", page),
                )
            ]
        )

        if total:
            text = (
                "📚 Готовые ответы\n\n"
                "Нажмите нужную группу — список ответов откроется только у вас.\n"
                "➕ справа — добавить ответ в эту группу."
            )
        else:
            text = (
                "📚 Готовые ответы\n\n"
                "Групп пока нет. Создайте первую — затем добавьте в неё ответы."
            )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    def _quick_reply_menu_guard(self) -> asyncio.Lock:
        lock = getattr(self, "_quick_reply_menu_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._quick_reply_menu_lock = lock
        return lock

    async def _write_quick_reply_menu(
        self,
        page: int,
        *,
        force_create: bool = False,
    ) -> None:
        service = self.quick_reply_service
        topic_id = self.quick_replies_topic_id
        assert service is not None
        assert topic_id is not None

        text, keyboard = await self._quick_reply_shared_menu(page)
        message_id = (
            None if force_create else await service.menu_message_id(self.settings.support_group_id)
        )
        if message_id is not None:
            try:
                await self.limiter.wait()
                await self.bot.edit_message_text(
                    chat_id=self.settings.support_group_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=None,
                )
            except TelegramBadRequest as error:
                if message_not_modified(error):
                    await self._pin_quick_reply_menu(message_id)
                    return
                if not message_missing(error):
                    raise
            else:
                await self._pin_quick_reply_menu(message_id)
                return

        await self.limiter.wait()
        message = await self.bot.send_message(
            chat_id=self.settings.support_group_id,
            message_thread_id=topic_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=None,
        )
        await service.save_menu_message_id(
            self.settings.support_group_id,
            message.message_id,
        )
        await self._pin_quick_reply_menu(message.message_id)

    async def _ensure_quick_reply_menu_locked(self, page: int) -> None:
        try:
            await self._write_quick_reply_menu(page)
        except TelegramBadRequest as error:
            if not is_missing_topic_error(error):
                raise
            missing_topic_id = self.quick_replies_topic_id
            recover_topic = getattr(self, "recover_quick_replies_topic", None)
            if missing_topic_id is None or recover_topic is None:
                raise RuntimeError("quick reply topic recovery is not configured") from error

            replacement_topic_id = await recover_topic(missing_topic_id)
            if replacement_topic_id <= 0:
                raise RuntimeError(
                    "quick reply topic recovery returned an invalid topic id"
                ) from error
            self.quick_replies_topic_id = replacement_topic_id
            await self.shutdown_quick_reply_sessions()
            await self._write_quick_reply_menu(page, force_create=True)
            logger.warning(
                "Recreated missing quick reply topic and menu",
                extra={
                    "event": "quick_reply_topic_recovered",
                    "old_topic_id": missing_topic_id,
                    "new_topic_id": replacement_topic_id,
                    "support_group_id": self.settings.support_group_id,
                },
            )

    async def ensure_quick_reply_menu(self, page: int = 0) -> None:
        service = getattr(self, "quick_reply_service", None)
        topic_id = getattr(self, "quick_replies_topic_id", None)
        if service is None or topic_id is None:
            return
        async with self._quick_reply_menu_guard():
            try:
                await self._ensure_quick_reply_menu_locked(page)
            except TelegramAPIError:
                logger.warning(
                    "Unable to initialize quick reply menu",
                    exc_info=True,
                    extra={"event": "quick_reply_menu_startup_degraded"},
                )
            except Exception:
                logger.exception(
                    "Unable to persist quick reply menu",
                    extra={"event": "quick_reply_menu_persistence_failed"},
                )

    async def refresh_quick_reply_menu(self, page: int = 0) -> None:
        service = getattr(self, "quick_reply_service", None)
        if service is None:
            return
        if await service.menu_message_id(self.settings.support_group_id) is None:
            return
        await self.ensure_quick_reply_menu(page)

    async def _shared_menu_message(
        self,
        callback: CallbackQuery,
        topic_id: int,
    ) -> Message | None:
        message = self._callback_message(callback, topic_id)
        if message is None or message.chat.id != self.settings.support_group_id:
            return None
        assert self.quick_reply_service is not None
        menu_message_id = await self.quick_reply_service.menu_message_id(
            self.settings.support_group_id
        )
        if menu_message_id != message.message_id:
            return None
        return message

    @staticmethod
    def _inline_reply_keyboard(
        owner_id: int,
        group_id: int,
        reply: QuickReplyView,
    ) -> InlineKeyboardMarkup:
        first_row: list[InlineKeyboardButton] = []
        if utf16_code_units(reply.text) <= TELEGRAM_COPY_TEXT_LIMIT:
            first_row.append(
                InlineKeyboardButton(
                    text="📋 Скопировать",
                    copy_text=CopyTextButton(text=reply.text),
                )
            )
        first_row.append(
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=callback_data("delete", owner_id),
            )
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[
                first_row,
                [
                    InlineKeyboardButton(
                        text="📚 Ещё из группы",
                        switch_inline_query_current_chat=inline_group_query(group_id),
                    )
                ],
            ]
        )

    @classmethod
    def _inline_reply_result(
        cls,
        owner_id: int,
        group_id: int,
        reply: QuickReplyView,
    ) -> InlineQueryResultArticle:
        return InlineQueryResultArticle(
            id=f"{group_id}:{reply.id}",
            title=reply.title,
            description=inline_reply_description(reply.text),
            input_message_content=InputTextMessageContent(
                message_text=reply.text,
                parse_mode=None,
            ),
            reply_markup=cls._inline_reply_keyboard(owner_id, group_id, reply),
        )

    @staticmethod
    async def _answer_empty_inline_query(inline_query: InlineQuery) -> None:
        await inline_query.answer(
            [],
            cache_time=0,
            is_personal=True,
            next_offset="",
        )

    async def handle_quick_reply_inline_query(self, inline_query: InlineQuery) -> None:
        if not self.authorization.is_admin(inline_query.from_user.id):
            await self._answer_empty_inline_query(inline_query)
            return
        service = getattr(self, "quick_reply_service", None)
        if service is None:
            await self._answer_empty_inline_query(inline_query)
            return
        group_id = parse_inline_group_query(inline_query.query)
        if group_id is None:
            await self._answer_empty_inline_query(inline_query)
            return
        try:
            offset = int(inline_query.offset or "0")
        except ValueError:
            await self._answer_empty_inline_query(inline_query)
            return
        if offset < 0 or await service.get_active_group(group_id) is None:
            await self._answer_empty_inline_query(inline_query)
            return

        replies, total = await service.list_active(
            group_id=group_id,
            offset=offset,
            limit=QUICK_REPLY_INLINE_PAGE_SIZE,
        )
        results: list[InlineQueryResultUnion] = [
            self._inline_reply_result(inline_query.from_user.id, group_id, reply)
            for reply in replies
        ]
        next_offset = (
            str(offset + len(replies)) if replies and offset + len(replies) < total else ""
        )
        await inline_query.answer(
            results,
            cache_time=0,
            is_personal=True,
            next_offset=next_offset,
        )

    async def _handle_add_group(self, message: Message) -> None:
        assert message.from_user is not None
        assert self.quick_reply_service is not None
        name = parse_add_group_argument(command_argument(message))
        if name is None:
            await message.reply(
                "⚠️ Формат:\n"
                "/addgroup Название группы\n\n"
                f"Название — до {QUICK_REPLY_GROUP_NAME_MAX_LENGTH} символов, "
                "символ | использовать нельзя."
            )
            return
        try:
            result = await self.quick_reply_service.create_group(
                name=name,
                operator_telegram_id=message.from_user.id,
                operator_display_name=message.from_user.full_name or None,
                operator_username=message.from_user.username,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
            )
        except QuickReplyGroupNameConflictError:
            await message.reply("⚠️ Группа с таким названием уже существует.")
            return
        await self._publish_group_if_needed(
            result.group,
            chat_id=message.chat.id,
            topic_id=message.message_thread_id,
        )
        await self.refresh_quick_reply_menu()
        await self._delete_quick_reply_message(message)

    async def _handle_add_answer(self, message: Message) -> None:
        assert message.from_user is not None
        assert self.quick_reply_service is not None
        parsed = parse_add_answer_argument(command_argument(message))
        if parsed is None:
            await message.reply(
                "⚠️ Формат:\n"
                "/addanswer Группа | Название кнопки\n"
                "Текст готового ответа\n\n"
                f"Группа — до {QUICK_REPLY_GROUP_NAME_MAX_LENGTH} символов, "
                f"название — до {QUICK_REPLY_TITLE_MAX_LENGTH}, "
                f"текст — до {QUICK_REPLY_TEXT_MAX_LENGTH}."
            )
            return
        group_name, title, text = parsed
        group = await self.quick_reply_service.get_active_group_by_name(group_name)
        if group is None:
            await message.reply(
                f"⚠️ Группа «{group_name}» не найдена. "
                "Используйте закреплённую панель, чтобы создать её."
            )
            return
        try:
            result = await self.quick_reply_service.create(
                group_id=group.id,
                title=title,
                text=text,
                operator_telegram_id=message.from_user.id,
                operator_display_name=message.from_user.full_name or None,
                operator_username=message.from_user.username,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
            )
        except QuickReplyTitleConflictError:
            await message.reply("⚠️ Готовый ответ с таким названием уже существует в этой группе.")
            return
        await self._publish_reply_if_needed(
            result.reply,
            group.name,
            chat_id=message.chat.id,
            topic_id=message.message_thread_id,
        )
        await self.refresh_quick_reply_menu()
        await self._delete_quick_reply_message(message)

    async def _handle_answers(self, message: Message) -> None:
        await self.ensure_quick_reply_menu()
        await self._delete_quick_reply_message(message)

    async def handle_quick_reply_topic_message(
        self,
        message: Message,
        command: str,
    ) -> bool:
        topic_id = getattr(self, "quick_replies_topic_id", None)
        service = getattr(self, "quick_reply_service", None)
        if topic_id is None or message.message_thread_id != topic_id:
            return False
        if service is None:
            await message.reply("⚠️ Готовые ответы временно недоступны.")
            return True
        if getattr(message, "via_bot", None) is not None:
            return True
        if not command.startswith("/") and await self._handle_draft_input(message):
            return True
        if command in {ADD_GROUP_COMMAND, ADD_ANSWER_COMMAND, ANSWERS_COMMAND}:
            if message.from_user is not None:
                await self._discard_draft(message.from_user.id, delete_panel=True)
        if command == ADD_GROUP_COMMAND:
            await self._handle_add_group(message)
        elif command == ADD_ANSWER_COMMAND:
            await self._handle_add_answer(message)
        elif command == ANSWERS_COMMAND:
            await self._handle_answers(message)
        elif command.startswith("/"):
            await message.reply("❓ Используйте кнопки в закреплённой панели готовых ответов.")
        return True

    @staticmethod
    def _callback_message(callback: CallbackQuery, topic_id: int) -> Message | None:
        message = callback.message
        if not isinstance(message, Message) or message.message_thread_id != topic_id:
            return None
        return message

    async def _handle_shared_callback(
        self,
        callback: CallbackQuery,
        action: str,
        parts: list[str],
        topic_id: int,
    ) -> bool:
        if action not in {"menupage", "add", "new"}:
            return False
        message = await self._shared_menu_message(callback, topic_id)
        if message is None:
            await callback.answer(
                "Панель устарела. Используйте актуальную закреплённую.",
                show_alert=True,
            )
            return True
        assert callback.from_user is not None
        assert self.quick_reply_service is not None

        if action == "menupage":
            try:
                page = int(parts[2])
            except (IndexError, ValueError):
                await callback.answer("Некорректная кнопка.", show_alert=False)
                return True
            text, keyboard = await self._quick_reply_shared_menu(page)
            try:
                await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            except TelegramBadRequest as error:
                if not message_not_modified(error):
                    raise
            await callback.answer()
            return True

        if action == "add":
            try:
                group_id = int(parts[2])
                page = int(parts[3])
            except (IndexError, ValueError):
                await callback.answer("Некорректная кнопка.", show_alert=False)
                return True
            group = await self.quick_reply_service.get_active_group(group_id)
            if group is None:
                await callback.answer("Группа не найдена.", show_alert=True)
                return True
            await self._start_title_draft(
                callback.from_user.id,
                group,
                max(0, page),
            )
            await callback.answer()
            return True

        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0
        await self._start_group_draft(callback.from_user.id, max(0, page))
        await callback.answer()
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
        except IndexError:
            await callback.answer("Некорректная кнопка.", show_alert=False)
            return

        if await self._handle_shared_callback(callback, action, parts, topic_id):
            return
        if action in _LEGACY_CATALOG_ACTIONS:
            await callback.answer(
                "Панель устарела. Используйте актуальную закреплённую.",
                show_alert=True,
            )
            return

        try:
            owner_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некорректная кнопка.", show_alert=False)
            return
        if owner_id != callback.from_user.id:
            await callback.answer(
                "Эта панель открыта другим оператором.",
                show_alert=True,
            )
            return

        message = self._callback_message(callback, topic_id)
        if message is None or message.chat.id != self.settings.support_group_id:
            await callback.answer("Панель больше недоступна.", show_alert=False)
            return

        if action == "delete":
            await self._delete_quick_reply_message(message)
            await callback.answer()
            return
        if action == "close":
            draft = self._draft_for_callback(owner_id, message.message_id)
            if draft is not None:
                await self._discard_draft(owner_id, delete_panel=False)
            await self._delete_quick_reply_message(message)
            await callback.answer()
            return
        if action == "draftcancel":
            draft = self._draft_for_callback(owner_id, message.message_id)
            if draft is not None:
                await self._discard_draft(owner_id, delete_panel=False)
            await self._delete_quick_reply_message(message)
            await callback.answer("Добавление отменено.")
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
                                callback_data=callback_data("delete", owner_id),
                            )
                        ]
                    ]
                ),
            )
            await callback.answer("Текст отправлен отдельным сообщением.")
            return
        if action in {"edittitle", "edittext", "draftsave"}:
            draft = self._draft_for_callback(owner_id, message.message_id)
            if draft is None:
                await callback.answer(
                    "Сессия завершена. Начните добавление заново.",
                    show_alert=True,
                )
                return
            if action == "edittitle":
                if draft.group_name is None:
                    await callback.answer("Сессия повреждена.", show_alert=True)
                    return
                await self._prompt_title(owner_id, draft)
                await callback.answer()
                return
            if action == "edittext":
                if draft.group_name is None or draft.title is None:
                    await callback.answer("Сначала укажите название.", show_alert=True)
                    return
                await self._prompt_text(owner_id, draft)
                await callback.answer()
                return
            if (
                draft.group_id is None
                or draft.group_name is None
                or draft.title is None
                or draft.text is None
                or draft.source_message_id is None
            ):
                await callback.answer("Ответ ещё не заполнен.", show_alert=True)
                return
            await self._save_draft(callback, message, draft)
            return
        if action == "again":
            try:
                group_id = int(parts[3])
            except (IndexError, ValueError):
                await callback.answer("Некорректная кнопка.", show_alert=False)
                return
            group = await service.get_active_group(group_id)
            if group is None:
                await callback.answer("Группа не найдена.", show_alert=True)
                return
            await self._begin_title_draft(
                owner_id,
                message.message_id,
                group,
                0,
            )
            await callback.answer()
            return
        await callback.answer("Некорректная кнопка.", show_alert=False)


class QuickReplyMenuRefreshWorker:
    def __init__(
        self,
        menu: TelegramQuickReplyHandlers,
        *,
        interval_seconds: float = QUICK_REPLY_MENU_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self.menu = menu
        self.interval_seconds = interval_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                try:
                    await self.menu.ensure_quick_reply_menu()
                except Exception:
                    logger.exception(
                        "Unable to refresh quick reply menu",
                        extra={"event": "quick_reply_menu_refresh_failed"},
                    )
