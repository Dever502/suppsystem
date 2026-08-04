from __future__ import annotations

import logging
import math

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
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
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.telegram_message_utils import command_argument
from suppsystem.telegram_quick_reply_drafts import TelegramQuickReplyDraftHandlers
from suppsystem.telegram_quick_reply_views import (
    ADD_ANSWER_COMMAND,
    ADD_GROUP_COMMAND,
    ANSWERS_COMMAND,
    QUICK_REPLY_PAGE_SIZE,
    TELEGRAM_COPY_TEXT_LIMIT,
    callback_data,
    message_missing,
    message_not_modified,
    parse_add_answer_argument,
    parse_add_group_argument,
    quick_reply_menu_keyboard,
    quick_reply_menu_text,
)
from suppsystem.telegram_quick_reply_views import (
    QUICK_REPLY_CALLBACK_PREFIX as QUICK_REPLY_CALLBACK_PREFIX,
)

logger = logging.getLogger(__name__)


class TelegramQuickReplyHandlers(TelegramQuickReplyDraftHandlers):
    bot: Bot
    authorization: AuthorizationService
    limiter: TelegramRateLimiter
    settings: Settings
    quick_reply_service: QuickReplyService | None
    quick_replies_topic_id: int | None

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

    async def ensure_quick_reply_menu(self) -> None:
        service = getattr(self, "quick_reply_service", None)
        topic_id = getattr(self, "quick_replies_topic_id", None)
        if service is None or topic_id is None:
            return
        try:
            message_id = await service.menu_message_id(self.settings.support_group_id)
            if message_id is not None:
                try:
                    await self.limiter.wait()
                    await self.bot.edit_message_text(
                        chat_id=self.settings.support_group_id,
                        message_id=message_id,
                        text=quick_reply_menu_text(),
                        reply_markup=quick_reply_menu_keyboard(),
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
                text=quick_reply_menu_text(),
                reply_markup=quick_reply_menu_keyboard(),
                parse_mode=None,
            )
            await service.save_menu_message_id(
                self.settings.support_group_id,
                message.message_id,
            )
            await self._pin_quick_reply_menu(message.message_id)
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

    @staticmethod
    def _group_button_text(group: QuickReplyGroupView) -> str:
        return f"📁 {group.name} · {group.reply_count}"

    async def _quick_reply_groups(
        self, owner_id: int, requested_page: int
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

        rows = [
            [
                InlineKeyboardButton(
                    text=self._group_button_text(group),
                    callback_data=callback_data("group", owner_id, group.id, 0, page),
                )
            ]
            for group in groups
        ]
        if pages > 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=callback_data("groups", owner_id, max(0, page - 1)),
                    ),
                    InlineKeyboardButton(
                        text=f"{page + 1}/{pages}",
                        callback_data=callback_data("groups", owner_id, page),
                    ),
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=callback_data(
                            "groups",
                            owner_id,
                            min(pages - 1, page + 1),
                        ),
                    ),
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Добавить готовый ответ",
                        callback_data=callback_data("addpicker", owner_id, 0),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Закрыть",
                        callback_data=callback_data("close", owner_id),
                    )
                ],
            ]
        )
        text = (
            "📚 Готовые ответы\n\nВыберите группу:"
            if total
            else "📚 Готовые ответы\n\nГрупп пока нет. Создайте первую при добавлении ответа."
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _quick_reply_group_picker(
        self, owner_id: int, requested_page: int
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

        rows = [
            [
                InlineKeyboardButton(
                    text=self._group_button_text(group),
                    callback_data=callback_data(
                        "draftselect",
                        owner_id,
                        group.id,
                        page,
                    ),
                )
            ]
            for group in groups
        ]
        if pages > 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=callback_data(
                            "addpicker",
                            owner_id,
                            max(0, page - 1),
                        ),
                    ),
                    InlineKeyboardButton(
                        text=f"{page + 1}/{pages}",
                        callback_data=callback_data("addpicker", owner_id, page),
                    ),
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=callback_data(
                            "addpicker",
                            owner_id,
                            min(pages - 1, page + 1),
                        ),
                    ),
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Создать новую группу",
                        callback_data=callback_data("draftnew", owner_id, page),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=callback_data("draftcancel", owner_id),
                    )
                ],
            ]
        )
        return (
            "➕ Новый готовый ответ\n\n"
            "Сначала выберите группу.\n"
            "Если подходящей нет — создайте новую.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _quick_reply_answers(
        self,
        owner_id: int,
        group: QuickReplyGroupView,
        requested_page: int,
        groups_page: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        assert self.quick_reply_service is not None
        page = max(0, requested_page)
        replies, total = await self.quick_reply_service.list_active(
            group_id=group.id,
            offset=page * QUICK_REPLY_PAGE_SIZE,
            limit=QUICK_REPLY_PAGE_SIZE,
        )
        pages = max(1, math.ceil(total / QUICK_REPLY_PAGE_SIZE))
        if page >= pages:
            page = pages - 1
            replies, total = await self.quick_reply_service.list_active(
                group_id=group.id,
                offset=page * QUICK_REPLY_PAGE_SIZE,
                limit=QUICK_REPLY_PAGE_SIZE,
            )

        rows = [
            [
                InlineKeyboardButton(
                    text=reply.title,
                    callback_data=callback_data(
                        "view",
                        owner_id,
                        reply.id,
                        page,
                        groups_page,
                    ),
                )
            ]
            for reply in replies
        ]
        if pages > 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅️",
                        callback_data=callback_data(
                            "group",
                            owner_id,
                            group.id,
                            max(0, page - 1),
                            groups_page,
                        ),
                    ),
                    InlineKeyboardButton(
                        text=f"{page + 1}/{pages}",
                        callback_data=callback_data(
                            "group",
                            owner_id,
                            group.id,
                            page,
                            groups_page,
                        ),
                    ),
                    InlineKeyboardButton(
                        text="➡️",
                        callback_data=callback_data(
                            "group",
                            owner_id,
                            group.id,
                            min(pages - 1, page + 1),
                            groups_page,
                        ),
                    ),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ К группам",
                    callback_data=callback_data("groups", owner_id, groups_page),
                ),
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data=callback_data("close", owner_id),
                ),
            ]
        )
        text = (
            f"📚 Готовые ответы → {group.name}\n\nВыберите нужный ответ:"
            if total
            else f"📚 Готовые ответы → {group.name}\n\nВ этой группе пока нет ответов."
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _quick_reply_preview(
        self,
        owner_id: int,
        group: QuickReplyGroupView,
        reply: QuickReplyView,
        page: int,
        groups_page: int,
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
                        callback_data=callback_data("text", owner_id, reply.id),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ К списку",
                    callback_data=callback_data(
                        "group",
                        owner_id,
                        reply.group_id,
                        page,
                        groups_page,
                    ),
                ),
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data=callback_data("close", owner_id),
                ),
            ]
        )
        return (
            f"📝 {group.name} → {reply.title}\n\n{reply.text}",
            InlineKeyboardMarkup(inline_keyboard=rows),
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
        await self._delete_quick_reply_message(message)

    async def _handle_answers(self, message: Message) -> None:
        assert message.from_user is not None
        text, keyboard = await self._quick_reply_groups(message.from_user.id, 0)
        await self.limiter.wait()
        await self.bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=None,
        )
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

    async def _send_personal_catalog(self, owner_id: int) -> None:
        await self._discard_draft(owner_id, delete_panel=True)
        text, keyboard = await self._quick_reply_groups(owner_id, 0)
        await self.limiter.wait()
        await self.bot.send_message(
            chat_id=self.settings.support_group_id,
            message_thread_id=self.quick_replies_topic_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def _send_group_picker(self, owner_id: int) -> None:
        await self._discard_draft(owner_id, delete_panel=True)
        text, keyboard = await self._quick_reply_group_picker(owner_id, 0)
        await self.limiter.wait()
        panel = await self.bot.send_message(
            chat_id=self.settings.support_group_id,
            message_thread_id=self.quick_replies_topic_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=None,
        )
        self._set_picker_draft(owner_id, panel.message_id, 0)

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

        if action in {"menu_catalog", "menu_add"}:
            message = self._callback_message(callback, topic_id)
            if message is None or message.chat.id != self.settings.support_group_id:
                await callback.answer("Панель больше недоступна.", show_alert=False)
                return
            if action == "menu_catalog":
                await self._send_personal_catalog(callback.from_user.id)
            else:
                await self._send_group_picker(callback.from_user.id)
            await callback.answer()
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
        if action == "groups":
            try:
                page = int(parts[3])
            except (IndexError, ValueError):
                page = 0
            text, keyboard = await self._quick_reply_groups(owner_id, page)
            await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            await callback.answer()
            return
        if action == "group":
            try:
                group_id = int(parts[3])
                page = int(parts[4])
                groups_page = int(parts[5])
            except (IndexError, ValueError):
                await callback.answer("Некорректная кнопка.", show_alert=False)
                return
            group = await service.get_active_group(group_id)
            if group is None:
                await callback.answer("Группа не найдена.", show_alert=True)
                return
            text, keyboard = await self._quick_reply_answers(
                owner_id,
                group,
                page,
                groups_page,
            )
            await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            await callback.answer()
            return
        if action == "view":
            try:
                reply_id = int(parts[3])
                page = int(parts[4])
                groups_page = int(parts[5])
            except (IndexError, ValueError):
                await callback.answer("Некорректная кнопка.", show_alert=False)
                return
            reply = await service.get_active(reply_id)
            if reply is None:
                await callback.answer("Ответ не найден.", show_alert=True)
                return
            group = await service.get_active_group(reply.group_id)
            if group is None:
                await callback.answer("Группа не найдена.", show_alert=True)
                return
            text, keyboard = await self._quick_reply_preview(
                owner_id,
                group,
                reply,
                page,
                groups_page,
            )
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
                                callback_data=callback_data("delete", owner_id),
                            )
                        ]
                    ]
                ),
            )
            await callback.answer(
                "Текст отправлен отдельным сообщением.",
                show_alert=False,
            )
            return
        if action == "addpicker":
            try:
                page = int(parts[3])
            except (IndexError, ValueError):
                page = 0
            draft = self._draft_for_callback(owner_id, message.message_id)
            if draft is not None:
                await self._discard_draft(owner_id, delete_panel=False)
            text, keyboard = await self._quick_reply_group_picker(owner_id, page)
            await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            self._set_picker_draft(owner_id, message.message_id, page)
            await callback.answer()
            return
        if action == "draftselect":
            try:
                group_id = int(parts[3])
                groups_page = int(parts[4])
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
                groups_page,
            )
            await callback.answer()
            return
        if action == "draftnew":
            try:
                groups_page = int(parts[3])
            except (IndexError, ValueError):
                groups_page = 0
            await self._begin_group_draft(
                owner_id,
                message.message_id,
                groups_page,
            )
            await callback.answer()
            return
        if action == "draftback":
            draft = self._draft_for_callback(owner_id, message.message_id)
            groups_page = draft.groups_page if draft is not None else 0
            if draft is not None:
                await self._discard_draft(owner_id, delete_panel=False)
            text, keyboard = await self._quick_reply_group_picker(
                owner_id,
                groups_page,
            )
            await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
            self._set_picker_draft(
                owner_id,
                message.message_id,
                groups_page,
            )
            await callback.answer()
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
                await self._prompt_title(owner_id, draft)
                await callback.answer()
                return
            if action == "edittext":
                await self._prompt_text(owner_id, draft)
                await callback.answer()
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
