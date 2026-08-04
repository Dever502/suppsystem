from __future__ import annotations

import logging
from html import escape

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from suppsystem.authorization import AuthorizationService
from suppsystem.media_storage import LocalMediaStorage, MediaValidationError, StoredMedia
from suppsystem.models import TicketChannel
from suppsystem.service_types import (
    TicketNotFoundError,
    TopicAlreadyBoundError,
    TopicProvisioningConflictError,
)
from suppsystem.telegram_constants import (
    COMMAND_ALREADY_HANDLED_TEXT,
    PANEL_MUTATION_COMMANDS,
    TICKET_CLOSED_TEXT,
    TOPIC_COMMANDS,
)
from suppsystem.telegram_formatting import internal_notes_text, operator_ticket_info
from suppsystem.telegram_message_utils import (
    command_argument,
    media_metadata,
    message_command,
    message_text,
    rating_keyboard,
)
from suppsystem.telegram_message_utils import (
    command_key as build_command_key,
)
from suppsystem.telegram_panel_handler import TelegramPanelCommandHandler
from suppsystem.telegram_quick_replies import TelegramQuickReplyHandlers
from suppsystem.telegram_statistics import TelegramStatisticsDashboard
from suppsystem.telegram_user_handlers import TelegramUserHandlers

logger = logging.getLogger(__name__)


class TelegramOperatorHandlers(
    TelegramQuickReplyHandlers, TelegramStatisticsDashboard, TelegramUserHandlers
):
    authorization: AuthorizationService
    panel_commands: TelegramPanelCommandHandler
    media_storage: LocalMediaStorage

    async def handle_edited_group_message(self, message: Message) -> None:
        actor = message.from_user
        if (
            actor is None
            or actor.is_bot
            or message.message_thread_id is None
            or not self.authorization.is_admin(actor.id)
        ):
            return
        await self.handle_quick_reply_topic_message(message)

    async def _delete_operator_media_if_unlinked(self, stored_media: StoredMedia) -> None:
        try:
            await self.ticket_service.get_media(stored_media.id)
        except TicketNotFoundError:
            await self.media_storage.delete(stored_media)
        except Exception:
            logger.warning(
                "Unable to determine whether operator media is linked; retaining it for cleanup",
                exc_info=True,
                extra={
                    "event": "web_operator_media_link_check_failed",
                    "media_id": stored_media.id,
                },
            )

    async def _handle_topic_rename_service_message(self, message: Message) -> bool:
        topic_edit = getattr(message, "forum_topic_edited", None)
        if topic_edit is None:
            return False
        actor = message.from_user
        new_name = topic_edit.name
        quick_response_topic_id = getattr(self, "quick_replies_topic_id", None)
        quick_response_topic = (
            quick_response_topic_id is not None
            and message.message_thread_id == quick_response_topic_id
        )
        if (
            actor is None
            or actor.id != self.bot.id
            or new_name is None
            or (not new_name.startswith(("🔴 ", "🟢 ")) and not quick_response_topic)
        ):
            return True
        try:
            await self.limiter.wait()
            await self.bot.delete_message(
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except TelegramAPIError:
            logger.warning(
                "Unable to remove bot topic rename service message",
                exc_info=True,
                extra={
                    "event": "topic_rename_service_message_delete_failed",
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "topic_id": message.message_thread_id,
                },
            )
        else:
            logger.info(
                "Removed bot topic rename service message",
                extra={
                    "event": "topic_rename_service_message_deleted",
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "topic_id": message.message_thread_id,
                },
            )
        return True

    async def handle_group_message(self, message: Message) -> None:
        if await self._handle_topic_rename_service_message(message):
            return
        if message.from_user is None or message.from_user.is_bot:
            return
        if message.message_thread_id is None:
            logger.info(
                "Received group root message",
                extra={
                    "event": "group_root_message_received",
                    "operator_telegram_id": message.from_user.id,
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                },
            )
            await self._handle_root_command(message)
            return
        if not self.authorization.is_admin(message.from_user.id):
            logger.info(
                "Ignored operator message without authorization",
                extra={
                    "event": "operator_message_unauthorized",
                    "operator_telegram_id": message.from_user.id,
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "topic_id": message.message_thread_id,
                },
            )
            return

        command = message_command(message)
        if await self.handle_quick_reply_topic_message(message, command):
            return
        ticket = await self.ticket_service.get_by_topic(message.message_thread_id)
        if ticket is None:
            logger.info(
                "Received message for unbound support topic",
                extra={
                    "event": "orphan_topic_message_received",
                    "operator_telegram_id": message.from_user.id,
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "topic_id": message.message_thread_id,
                },
            )
            await self._handle_orphan_topic_command(message, command)
            return
        logger.info(
            "Received operator topic message",
            extra={
                "event": "operator_topic_message_received",
                "ticket_id": ticket.id,
                "operator_telegram_id": message.from_user.id,
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "topic_id": message.message_thread_id,
                "command": command if command.startswith("/") else None,
            },
        )
        if command.startswith("/") and command not in TOPIC_COMMANDS:
            logger.info(
                "Blocked unknown operator command",
                extra={
                    "event": "operator_unknown_command_blocked",
                    "ticket_id": ticket.id,
                    "operator_telegram_id": message.from_user.id,
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "topic_id": message.message_thread_id,
                    "command": command,
                },
            )
            await message.reply(
                "❓ <b>Неизвестная команда</b>\n\n"
                f"<code>{escape(command)}</code> не выполнена. Сообщение не отправлено клиенту."
            )
            return

        command_key = build_command_key(message, command)
        if command == "/info":
            await message.reply(operator_ticket_info(ticket))
            return
        if command == "/subinfo":
            await message.reply(await self._subscription_block(ticket))
            return
        if command == "/notes":
            notes = await self.ticket_service.list_internal_notes(ticket.id)
            await message.reply(internal_notes_text(notes))
            return
        if command == "/resolvepanel":
            if self.panel_service is None:
                await message.reply("⚠️ Интеграция с Remnawave не подключена.")
                return
            arguments = command_argument(message).split()
            if len(arguments) != 2 or arguments[1] not in {"applied", "not_applied"}:
                await message.reply(
                    "⚠️ Формат: <code>/resolvepanel &lt;action_uuid&gt; applied|not_applied</code>"
                )
                return
            changed = await self.panel_service.resolve_inconclusive_action(
                ticket_id=ticket.id,
                operator_action_id=arguments[0],
                operator_telegram_id=message.from_user.id,
                resolution=arguments[1],
                idempotency_key=command_key,
            )
            await message.reply(
                "✅ Результат Remnawave зафиксирован; новые команды разблокированы."
                if changed
                else "ℹ️ Действие не найдено, уже разрешено или ещё сверяется автоматически."
            )
            return
        if command in PANEL_MUTATION_COMMANDS:
            await self.panel_commands.handle(
                message,
                ticket,
                command,
                command_key,
                command_argument(message),
            )
            return
        if command == "/note":
            note = command_argument(message)
            if not note:
                await message.reply(
                    "⚠️ <b>Не указан текст заметки</b>\n\nПример: <code>/note Текст заметки</code>"
                )
                return
            changed = await self.ticket_service.add_internal_note(
                ticket_id=ticket.id,
                operator_telegram_id=message.from_user.id,
                operator_display_name=message.from_user.full_name or None,
                operator_username=message.from_user.username,
                note=note,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                idempotency_key=command_key,
            )
            await message.reply(
                "✅ Заметка добавлена." if changed else COMMAND_ALREADY_HANDLED_TEXT
            )
            return

        if command in {"/stop", "/hidestop"}:
            if ticket.status.value == "closed":
                await message.reply("ℹ️ Тикет уже закрыт.")
                return
            notify_user = command == "/stop"
            rating_ticket_id = ticket.id
            changed = await self.ticket_service.close(
                ticket_id=ticket.id,
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
                notification_text=TICKET_CLOSED_TEXT if notify_user else None,
                notification_target_chat_id=ticket.telegram_user_id if notify_user else None,
                notification_idempotency_key=f"{command_key}:user-notification",
                notification_parse_mode="HTML" if notify_user else None,
                notification_reply_markup_builder=(
                    (
                        lambda close_cycle: rating_keyboard(
                            rating_ticket_id, close_cycle
                        ).model_dump(mode="json", exclude_none=True)
                    )
                    if notify_user
                    else None
                ),
            )
            if not changed:
                await message.reply(COMMAND_ALREADY_HANDLED_TEXT)
                return
            await message.reply(
                "✅ <b>Тикет закрыт</b>\n\nПользователь получит уведомление."
                if notify_user
                else "✅ <b>Тикет закрыт</b>\n\nУведомление пользователю не отправлено."
            )
            return
        if command == "/closeall":
            closed = await self.ticket_service.close_all(
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
            )
            await message.reply(f"✅ Закрыто тикетов: <b>{len(closed)}</b>.")
            return
        if command == "/synctopics":
            queued = await self.ticket_service.queue_all_topic_reconciliations()
            await message.reply(f"✅ Темы поставлены на синхронизацию: <b>{queued}</b>.")
            return
        if command == "/block":
            changed = await self.ticket_service.block_ticket(
                ticket_id=ticket.id,
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
            )
            await message.reply(
                "⛔ Пользователь заблокирован." if changed else COMMAND_ALREADY_HANDLED_TEXT
            )
            return
        if command == "/unblock":
            changed = await self.ticket_service.unblock_ticket(
                ticket_id=ticket.id,
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
            )
            await message.reply(
                "✅ Пользователь разблокирован." if changed else COMMAND_ALREADY_HANDLED_TEXT
            )
            return
        if command.startswith("/"):
            await message.reply("❓ Неизвестная команда. Сообщение не отправлено клиенту.")
            return

        stored_media: StoredMedia | None = None
        operator_media = media_metadata(message)
        if getattr(ticket, "channel", TicketChannel.TELEGRAM) is TicketChannel.WEB:
            content_type = getattr(message.content_type, "value", str(message.content_type))
            if content_type not in {"text", "photo"}:
                await message.reply("⚠️ Для Web-клиента сейчас поддерживаются только текст и фото.")
                return
            if content_type == "photo":
                photos = message.photo or []
                if not photos:
                    await message.reply("⚠️ Не удалось прочитать фотографию.")
                    return
                try:
                    stored_media = await self.media_storage.save_telegram_photo(
                        self.bot, file_id=photos[-1].file_id
                    )
                except MediaValidationError:
                    await message.reply("⚠️ Фотография не прошла проверку.")
                    return
                except Exception:
                    logger.exception(
                        "Unable to persist operator photo for Web client",
                        extra={"event": "web_operator_photo_store_failed", "ticket_id": ticket.id},
                    )
                    await message.reply("⚠️ Не удалось сохранить фотографию для Web-клиента.")
                    return
                operator_media = stored_media.message_metadata()
        try:
            result = await self.ticket_service.accept_operator_reply(
                ticket_id=ticket.id,
                operator_telegram_id=message.from_user.id,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                content=message_text(message),
                media=operator_media,
                stored_media=stored_media,
            )
        except Exception:
            if stored_media is not None:
                await self._delete_operator_media_if_unlinked(stored_media)
            raise
        if stored_media is not None and not result.changed:
            await self.media_storage.delete(stored_media)
        if result.blocked:
            await message.reply("⛔ Пользователь заблокирован. Сообщение не отправлено.")
            return
        queued = result.changed
        if result.ticket is not None:
            ticket = result.ticket
        if result.reopened:
            await self._send_ticket_reopened_notice(ticket, by_operator=True)
            await self._send_reopened_ticket_customer_card(ticket)
        logger.info(
            "Queued operator reply for delivery" if queued else "Duplicate operator reply ignored",
            extra={
                "event": "operator_reply_queued" if queued else "operator_reply_duplicate",
                "ticket_id": ticket.id,
                "telegram_user_id": ticket.telegram_user_id,
                "operator_telegram_id": message.from_user.id,
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "topic_id": message.message_thread_id,
            },
        )

    async def _handle_root_command(self, message: Message) -> None:
        if message.from_user is None or not self.authorization.is_admin(message.from_user.id):
            return
        parts = (message.text or "").split()
        command = parts[0].lower().split("@", maxsplit=1)[0] if parts else ""
        if command != "/retrytopic" or len(parts) != 2:
            return
        changed = await self.ticket_service.reset_topic_provisioning(parts[1])
        await message.reply(
            "✅ Восстановление темы разрешено. Следующее сообщение создаст новую тему."
            if changed
            else "ℹ️ Для этого тикета восстановление темы не требуется."
        )

    async def _handle_orphan_topic_command(self, message: Message, command: str) -> None:
        if message.from_user is None or not self.authorization.is_admin(message.from_user.id):
            return
        parts = (message.text or "").split()
        if command != "/bindtopic" or len(parts) != 2 or message.message_thread_id is None:
            return
        ticket_id = parts[1]
        token: str | None = None
        try:
            target = await self.ticket_service.get_ticket(ticket_id)
            async with self._ticket_locks.hold(
                getattr(target, "lock_key", str(target.telegram_user_id))
            ):
                # /bindtopic is an explicit administrator recovery decision. Cancel
                # any uncertain automatic claim, then attach only under a fresh
                # token so a late automatic result cannot overwrite this binding.
                await self.ticket_service.reset_topic_provisioning(ticket_id)
                token = await self.ticket_service.claim_topic_provisioning(ticket_id)
                if token is None:
                    await message.reply(
                        "⚠️ Тема не привязана: тикет закрыт, уже привязан или занят восстановлением."
                    )
                    return
                ticket = await self.ticket_service.attach_topic(
                    ticket_id,
                    message.message_thread_id,
                    token=token,
                )
        except (
            TicketNotFoundError,
            TopicAlreadyBoundError,
            TopicProvisioningConflictError,
        ):
            if token is not None:
                await self.ticket_service.abort_topic_provisioning(
                    ticket_id=ticket_id,
                    token=token,
                )
            await message.reply("⚠️ Не удалось привязать тему к указанному тикету.")
            return
        await self._sync_ticket_topic(ticket)
        await message.reply("✅ Тема привязана к тикету.")
