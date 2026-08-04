from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from suppsystem.config import Settings
from suppsystem.quick_replies import (
    QUICK_REPLY_GROUP_NAME_MAX_LENGTH,
    QUICK_REPLY_TEXT_MAX_LENGTH,
    QUICK_REPLY_TITLE_MAX_LENGTH,
    QuickReplyGroupNameConflictError,
    QuickReplyGroupNotFoundError,
    QuickReplyGroupView,
    QuickReplyService,
    QuickReplyTitleConflictError,
    QuickReplyView,
    utf16_code_units,
)
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.telegram_quick_reply_views import (
    QUICK_REPLY_DRAFT_TIMEOUT_SECONDS,
    TELEGRAM_COPY_TEXT_LIMIT,
    callback_data,
    clean_draft_text,
    clean_draft_title,
    parse_add_group_argument,
    quick_reply_card,
    quick_reply_group_card,
)

logger = logging.getLogger(__name__)

DraftStep = Literal["pick", "group", "title", "text", "confirm"]


@dataclass
class QuickReplyDraft:
    panel_message_id: int
    groups_page: int
    step: DraftStep
    group_id: int | None = None
    group_name: str | None = None
    title: str | None = None
    text: str | None = None
    source_message_id: int | None = None
    prompt_message_id: int | None = None


class TelegramQuickReplyDraftHandlers:
    bot: Bot
    limiter: TelegramRateLimiter
    settings: Settings
    quick_reply_service: QuickReplyService | None
    quick_replies_topic_id: int | None
    _quick_reply_drafts: dict[int, QuickReplyDraft]
    _quick_reply_draft_tasks: dict[int, asyncio.Task[None]]

    def initialize_quick_reply_sessions(self) -> None:
        self._quick_reply_drafts = {}
        self._quick_reply_draft_tasks = {}

    def _ensure_quick_reply_sessions(self) -> None:
        if not hasattr(self, "_quick_reply_drafts"):
            self.initialize_quick_reply_sessions()

    async def shutdown_quick_reply_sessions(self) -> None:
        self._ensure_quick_reply_sessions()
        tasks = list(self._quick_reply_draft_tasks.values())
        self._quick_reply_draft_tasks.clear()
        self._quick_reply_drafts.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _set_draft(self, owner_id: int, draft: QuickReplyDraft) -> None:
        self._ensure_quick_reply_sessions()
        previous_task = self._quick_reply_draft_tasks.pop(owner_id, None)
        if previous_task is not None:
            previous_task.cancel()
        self._quick_reply_drafts[owner_id] = draft
        self._quick_reply_draft_tasks[owner_id] = asyncio.create_task(
            self._expire_draft(owner_id, draft),
            name=f"quick-reply-draft-timeout-{owner_id}",
        )

    def _set_picker_draft(
        self,
        owner_id: int,
        panel_message_id: int,
        groups_page: int,
    ) -> None:
        self._set_draft(
            owner_id,
            QuickReplyDraft(
                panel_message_id=panel_message_id,
                groups_page=groups_page,
                step="pick",
            ),
        )

    def _pop_draft(self, owner_id: int) -> QuickReplyDraft | None:
        self._ensure_quick_reply_sessions()
        draft = self._quick_reply_drafts.pop(owner_id, None)
        task = self._quick_reply_draft_tasks.pop(owner_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        return draft

    async def _expire_draft(self, owner_id: int, draft: QuickReplyDraft) -> None:
        try:
            await asyncio.sleep(QUICK_REPLY_DRAFT_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        self._ensure_quick_reply_sessions()
        if self._quick_reply_drafts.get(owner_id) is not draft:
            return
        self._quick_reply_drafts.pop(owner_id, None)
        self._quick_reply_draft_tasks.pop(owner_id, None)
        if draft.prompt_message_id is not None:
            await self._delete_quick_reply_message_id(draft.prompt_message_id)
        await self._delete_quick_reply_message_id(draft.panel_message_id)

    async def _delete_quick_reply_message_id(self, message_id: int) -> None:
        try:
            await self.limiter.wait()
            await self.bot.delete_message(
                chat_id=self.settings.support_group_id,
                message_id=message_id,
            )
        except TelegramAPIError:
            logger.info(
                "Unable to delete transient quick reply message",
                exc_info=True,
                extra={
                    "event": "quick_reply_transient_delete_failed",
                    "chat_id": self.settings.support_group_id,
                    "message_id": message_id,
                },
            )

    async def _delete_quick_reply_message(self, message: Message) -> None:
        await self._delete_quick_reply_message_id(message.message_id)

    async def _discard_draft(
        self,
        owner_id: int,
        *,
        delete_panel: bool,
    ) -> QuickReplyDraft | None:
        draft = self._pop_draft(owner_id)
        if draft is None:
            return None
        if draft.prompt_message_id is not None:
            await self._delete_quick_reply_message_id(draft.prompt_message_id)
        if delete_panel:
            await self._delete_quick_reply_message_id(draft.panel_message_id)
        return draft

    async def _edit_draft_panel(
        self,
        draft: QuickReplyDraft,
        *,
        text: str,
        keyboard: InlineKeyboardMarkup,
    ) -> None:
        await self.limiter.wait()
        await self.bot.edit_message_text(
            chat_id=self.settings.support_group_id,
            message_id=draft.panel_message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=None,
        )

    async def _send_draft_prompt(
        self,
        owner_id: int,
        draft: QuickReplyDraft,
        *,
        text: str,
        placeholder: str,
    ) -> None:
        if draft.prompt_message_id is not None:
            await self._delete_quick_reply_message_id(draft.prompt_message_id)
        await self.limiter.wait()
        prompt = await self.bot.send_message(
            chat_id=self.settings.support_group_id,
            message_thread_id=self.quick_replies_topic_id,
            text=text,
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder=placeholder,
            ),
            parse_mode=None,
        )
        draft.prompt_message_id = prompt.message_id
        self._set_draft(owner_id, draft)

    @staticmethod
    def _draft_cancel_keyboard(
        owner_id: int,
        *,
        back_action: str,
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=callback_data(back_action, owner_id),
                    ),
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=callback_data("draftcancel", owner_id),
                    ),
                ]
            ]
        )

    async def _prompt_group_name(
        self,
        owner_id: int,
        draft: QuickReplyDraft,
        *,
        error: str | None = None,
    ) -> None:
        draft.step = "group"
        status = f"\n\n⚠️ {error}" if error else ""
        await self._edit_draft_panel(
            draft,
            text=(
                "➕ Новая группа\n\n"
                "Отправьте название группы одним сообщением.\n"
                f"Максимум {QUICK_REPLY_GROUP_NAME_MAX_LENGTH} символов.{status}"
            ),
            keyboard=self._draft_cancel_keyboard(
                owner_id,
                back_action="draftback",
            ),
        )
        await self._send_draft_prompt(
            owner_id,
            draft,
            text="Введите название новой группы:",
            placeholder="Например: НЕ РАБОТАЕТ AI",
        )

    async def _prompt_title(
        self,
        owner_id: int,
        draft: QuickReplyDraft,
        *,
        error: str | None = None,
    ) -> None:
        assert draft.group_name is not None
        draft.step = "title"
        status = f"\n\n⚠️ {error}" if error else ""
        await self._edit_draft_panel(
            draft,
            text=(
                "➕ Новый готовый ответ\n\n"
                f"Группа: {draft.group_name}\n\n"
                "Шаг 1 из 2. Отправьте короткое название.\n"
                "Оно будет показано на кнопке.\n"
                f"Максимум {QUICK_REPLY_TITLE_MAX_LENGTH} символов.{status}"
            ),
            keyboard=self._draft_cancel_keyboard(
                owner_id,
                back_action="draftback",
            ),
        )
        await self._send_draft_prompt(
            owner_id,
            draft,
            text="Введите название кнопки:",
            placeholder="Например: AI не отвечает",
        )

    async def _prompt_text(
        self,
        owner_id: int,
        draft: QuickReplyDraft,
        *,
        error: str | None = None,
    ) -> None:
        assert draft.group_name is not None
        assert draft.title is not None
        draft.step = "text"
        status = f"\n\n⚠️ {error}" if error else ""
        await self._edit_draft_panel(
            draft,
            text=(
                "➕ Новый готовый ответ\n\n"
                f"Группа: {draft.group_name}\n"
                f"Название: {draft.title}\n\n"
                "Шаг 2 из 2. Отправьте полный текст ответа.\n"
                f"Максимум {QUICK_REPLY_TEXT_MAX_LENGTH} символов.{status}"
            ),
            keyboard=self._draft_cancel_keyboard(
                owner_id,
                back_action="edittitle",
            ),
        )
        await self._send_draft_prompt(
            owner_id,
            draft,
            text="Отправьте текст готового ответа:",
            placeholder="Текст, который оператор будет копировать",
        )

    async def _show_draft_confirmation(
        self,
        owner_id: int,
        draft: QuickReplyDraft,
    ) -> None:
        assert draft.group_name is not None
        assert draft.title is not None
        assert draft.text is not None
        draft.step = "confirm"
        draft.prompt_message_id = None
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Сохранить",
                        callback_data=callback_data("draftsave", owner_id),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить название",
                        callback_data=callback_data("edittitle", owner_id),
                    ),
                    InlineKeyboardButton(
                        text="✏️ Изменить текст",
                        callback_data=callback_data("edittext", owner_id),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=callback_data("draftcancel", owner_id),
                    )
                ],
            ]
        )
        await self._edit_draft_panel(
            draft,
            text=(
                "Новый готовый ответ\n\n"
                f"Группа: {draft.group_name}\n"
                f"Название: {draft.title}\n\n"
                f"{draft.text}"
            ),
            keyboard=keyboard,
        )
        self._set_draft(owner_id, draft)

    async def _publish_group_if_needed(
        self,
        group: QuickReplyGroupView,
        *,
        chat_id: int,
        topic_id: int | None,
    ) -> None:
        assert self.quick_reply_service is not None
        if group.published_message_id is not None:
            return
        await self.limiter.wait()
        published = await self.bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=quick_reply_group_card(group),
            parse_mode=None,
        )
        if not await self.quick_reply_service.mark_group_published(
            group.id,
            published.message_id,
        ):
            await self._delete_quick_reply_message(published)

    async def _publish_reply_if_needed(
        self,
        reply: QuickReplyView,
        group_name: str,
        *,
        chat_id: int,
        topic_id: int | None,
    ) -> None:
        assert self.quick_reply_service is not None
        if reply.published_message_id is not None:
            return
        await self.limiter.wait()
        published = await self.bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=quick_reply_card(reply, group_name),
            parse_mode=None,
        )
        if not await self.quick_reply_service.mark_published(
            reply.id,
            published.message_id,
        ):
            await self._delete_quick_reply_message(published)

    async def _handle_draft_group_name(
        self,
        message: Message,
        draft: QuickReplyDraft,
        value: str,
    ) -> None:
        assert message.from_user is not None
        assert self.quick_reply_service is not None
        name = parse_add_group_argument(value)
        if name is None:
            await self._prompt_group_name(
                message.from_user.id,
                draft,
                error=(
                    "Название должно быть одной строкой без символа | "
                    f"и не длиннее {QUICK_REPLY_GROUP_NAME_MAX_LENGTH} символов."
                ),
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
            group = result.group
        except QuickReplyGroupNameConflictError:
            existing = await self.quick_reply_service.get_active_group_by_name(name)
            if existing is None:
                await self._prompt_group_name(
                    message.from_user.id,
                    draft,
                    error="Группа с таким названием уже существует, но сейчас недоступна.",
                )
                return
            group = existing
        await self._publish_group_if_needed(
            group,
            chat_id=message.chat.id,
            topic_id=message.message_thread_id,
        )
        draft.group_id = group.id
        draft.group_name = group.name
        draft.prompt_message_id = None
        await self._prompt_title(message.from_user.id, draft)

    async def _handle_draft_input(self, message: Message) -> bool:
        if message.from_user is None:
            return False
        self._ensure_quick_reply_sessions()
        draft = self._quick_reply_drafts.get(message.from_user.id)
        if draft is None:
            return False
        if draft.step == "pick":
            return False
        value = message.text or message.caption
        if not value:
            await self._delete_quick_reply_message(message)
            if draft.step == "group":
                await self._prompt_group_name(
                    message.from_user.id,
                    draft,
                    error="Отправьте название обычным текстовым сообщением.",
                )
            elif draft.step == "title":
                await self._prompt_title(
                    message.from_user.id,
                    draft,
                    error="Отправьте название обычным текстовым сообщением.",
                )
            elif draft.step == "text":
                await self._prompt_text(
                    message.from_user.id,
                    draft,
                    error="Отправьте готовый ответ обычным текстовым сообщением.",
                )
            return True

        if draft.prompt_message_id is not None:
            await self._delete_quick_reply_message_id(draft.prompt_message_id)
            draft.prompt_message_id = None
        await self._delete_quick_reply_message(message)

        if draft.step == "group":
            await self._handle_draft_group_name(message, draft, value)
            return True
        if draft.step == "title":
            title = clean_draft_title(value)
            if title is None:
                await self._prompt_title(
                    message.from_user.id,
                    draft,
                    error=(
                        "Название не может быть пустым или длиннее "
                        f"{QUICK_REPLY_TITLE_MAX_LENGTH} символов."
                    ),
                )
                return True
            draft.title = title
            await self._prompt_text(message.from_user.id, draft)
            return True
        if draft.step == "text":
            clean_text = clean_draft_text(value)
            if clean_text is None:
                await self._prompt_text(
                    message.from_user.id,
                    draft,
                    error=(
                        "Текст не может быть пустым или длиннее "
                        f"{QUICK_REPLY_TEXT_MAX_LENGTH} символов."
                    ),
                )
                return True
            draft.text = clean_text
            draft.source_message_id = message.message_id
            await self._show_draft_confirmation(message.from_user.id, draft)
            return True
        return True

    def _draft_for_callback(
        self,
        owner_id: int,
        panel_message_id: int,
    ) -> QuickReplyDraft | None:
        self._ensure_quick_reply_sessions()
        draft = self._quick_reply_drafts.get(owner_id)
        if draft is None or draft.panel_message_id != panel_message_id:
            return None
        return draft

    async def _begin_title_draft(
        self,
        owner_id: int,
        panel_message_id: int,
        group: QuickReplyGroupView,
        groups_page: int,
    ) -> None:
        existing = (
            self._quick_reply_drafts.get(owner_id) if hasattr(self, "_quick_reply_drafts") else None
        )
        await self._discard_draft(
            owner_id,
            delete_panel=(existing is not None and existing.panel_message_id != panel_message_id),
        )
        draft = QuickReplyDraft(
            panel_message_id=panel_message_id,
            groups_page=groups_page,
            step="title",
            group_id=group.id,
            group_name=group.name,
        )
        await self._prompt_title(owner_id, draft)

    async def _begin_group_draft(
        self,
        owner_id: int,
        panel_message_id: int,
        groups_page: int,
    ) -> None:
        existing = (
            self._quick_reply_drafts.get(owner_id) if hasattr(self, "_quick_reply_drafts") else None
        )
        await self._discard_draft(
            owner_id,
            delete_panel=(existing is not None and existing.panel_message_id != panel_message_id),
        )
        draft = QuickReplyDraft(
            panel_message_id=panel_message_id,
            groups_page=groups_page,
            step="group",
        )
        await self._prompt_group_name(owner_id, draft)

    async def _save_draft(
        self,
        callback: CallbackQuery,
        message: Message,
        draft: QuickReplyDraft,
    ) -> None:
        assert callback.from_user is not None
        assert self.quick_reply_service is not None
        assert draft.group_id is not None
        assert draft.group_name is not None
        assert draft.title is not None
        assert draft.text is not None
        assert draft.source_message_id is not None
        try:
            result = await self.quick_reply_service.create(
                group_id=draft.group_id,
                title=draft.title,
                text=draft.text,
                operator_telegram_id=callback.from_user.id,
                operator_display_name=callback.from_user.full_name or None,
                operator_username=callback.from_user.username,
                source_chat_id=message.chat.id,
                source_message_id=draft.source_message_id,
            )
        except QuickReplyTitleConflictError:
            await message.edit_text(
                "⚠️ В этой группе уже есть ответ с таким названием.\n\n"
                "Измените название и сохраните ещё раз.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✏️ Изменить название",
                                callback_data=callback_data(
                                    "edittitle",
                                    callback.from_user.id,
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Отмена",
                                callback_data=callback_data(
                                    "draftcancel",
                                    callback.from_user.id,
                                ),
                            )
                        ],
                    ]
                ),
                parse_mode=None,
            )
            await callback.answer("Название уже используется.", show_alert=True)
            return
        except QuickReplyGroupNotFoundError:
            self._pop_draft(callback.from_user.id)
            await message.edit_text(
                "⚠️ Группа больше недоступна. Начните добавление заново.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="➕ Добавить готовый ответ",
                                callback_data=callback_data(
                                    "addpicker",
                                    callback.from_user.id,
                                    0,
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Закрыть",
                                callback_data=callback_data(
                                    "close",
                                    callback.from_user.id,
                                ),
                            )
                        ],
                    ]
                ),
                parse_mode=None,
            )
            await callback.answer("Группа недоступна.", show_alert=True)
            return

        await self._publish_reply_if_needed(
            result.reply,
            draft.group_name,
            chat_id=message.chat.id,
            topic_id=message.message_thread_id,
        )
        self._pop_draft(callback.from_user.id)
        rows: list[list[InlineKeyboardButton]] = []
        if utf16_code_units(result.reply.text) <= TELEGRAM_COPY_TEXT_LIMIT:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📋 Скопировать",
                        copy_text=CopyTextButton(text=result.reply.text),
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📄 Показать чистый текст",
                        callback_data=callback_data(
                            "text",
                            callback.from_user.id,
                            result.reply.id,
                        ),
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Добавить ещё",
                        callback_data=callback_data(
                            "again",
                            callback.from_user.id,
                            draft.group_id,
                        ),
                    ),
                    InlineKeyboardButton(
                        text="📖 Открыть группу",
                        callback_data=callback_data(
                            "group",
                            callback.from_user.id,
                            draft.group_id,
                            0,
                            draft.groups_page,
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Закрыть",
                        callback_data=callback_data(
                            "close",
                            callback.from_user.id,
                        ),
                    )
                ],
            ]
        )
        await message.edit_text(
            f"✅ Готовый ответ сохранён\n\nГруппа: {draft.group_name}\nНазвание: {draft.title}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )
        await callback.answer("Готовый ответ сохранён.")
