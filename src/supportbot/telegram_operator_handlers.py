from __future__ import annotations

import logging
from html import escape

from aiogram.types import Message

from supportbot.authorization import AuthorizationService
from supportbot.service_types import (
    TicketNotFoundError,
    TopicAlreadyBoundError,
    TopicProvisioningConflictError,
)
from supportbot.telegram_constants import (
    COMMAND_ALREADY_HANDLED_TEXT,
    PANEL_MUTATION_COMMANDS,
    TICKET_CLOSED_TEXT,
    TOPIC_COMMANDS,
)
from supportbot.telegram_formatting import internal_notes_text, operator_ticket_info
from supportbot.telegram_message_utils import (
    command_argument,
    media_metadata,
    message_command,
    message_text,
    rating_keyboard,
)
from supportbot.telegram_message_utils import (
    command_key as build_command_key,
)
from supportbot.telegram_panel_handler import TelegramPanelCommandHandler
from supportbot.telegram_user_handlers import TelegramUserHandlers

logger = logging.getLogger(__name__)


class TelegramOperatorHandlers(TelegramUserHandlers):
    authorization: AuthorizationService
    panel_commands: TelegramPanelCommandHandler

    async def handle_group_message(self, message: Message) -> None:
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
        if not self.authorization.can_read(message.from_user.id):
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
        if not self.authorization.can_execute_topic_action(message.from_user.id, command):
            logger.info(
                "Blocked read-only operator action",
                extra={
                    "event": "readonly_operator_action_blocked",
                    "operator_telegram_id": message.from_user.id,
                    "topic_id": message.message_thread_id,
                    "command": command if command.startswith("/") else None,
                },
            )
            await message.reply("⛔ Роль только для чтения. Действие не выполнено.")
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
            if not self.authorization.is_full_admin(message.from_user.id):
                await message.reply("⛔ Только full admin может разрешить неизвестный результат.")
                return
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
            changed = await self.ticket_service.close(
                ticket_id=ticket.id,
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
                notification_text=TICKET_CLOSED_TEXT if notify_user else None,
                notification_target_chat_id=ticket.telegram_user_id if notify_user else None,
                notification_idempotency_key=f"{command_key}:user-notification",
                notification_reply_markup=(
                    rating_keyboard(ticket.id, ticket.close_cycle + 1).model_dump(
                        mode="json", exclude_none=True
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
        if command in {"/stopall", "/synctopics", "/block", "/unblock"}:
            if not self.authorization.has_full_access(message.from_user.id):
                await message.reply("⛔ Недостаточно прав для этой команды.")
                return
        if command == "/stopall":
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
            changed = await self.ticket_service.block(
                telegram_user_id=ticket.telegram_user_id,
                operator_telegram_id=message.from_user.id,
                ticket_id=ticket.id,
                idempotency_key=command_key,
            )
            await message.reply(
                "⛔ Пользователь заблокирован." if changed else COMMAND_ALREADY_HANDLED_TEXT
            )
            return
        if command == "/unblock":
            changed = await self.ticket_service.unblock(
                telegram_user_id=ticket.telegram_user_id,
                operator_telegram_id=message.from_user.id,
                ticket_id=ticket.id,
                idempotency_key=command_key,
            )
            await message.reply(
                "✅ Пользователь разблокирован." if changed else COMMAND_ALREADY_HANDLED_TEXT
            )
            return
        if command.startswith("/"):
            await message.reply("❓ Неизвестная команда. Сообщение не отправлено клиенту.")
            return

        result = await self.ticket_service.accept_operator_reply(
            ticket_id=ticket.id,
            operator_telegram_id=message.from_user.id,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            content=message_text(message),
            media=media_metadata(message),
        )
        if result.blocked:
            await message.reply("⛔ Пользователь заблокирован. Сообщение не отправлено.")
            return
        queued = result.changed
        if result.ticket is not None:
            ticket = result.ticket
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
        if message.from_user is None or not self.authorization.has_full_access(
            message.from_user.id
        ):
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
        if message.from_user is None or not self.authorization.has_full_access(
            message.from_user.id
        ):
            return
        parts = (message.text or "").split()
        if command != "/bindtopic" or len(parts) != 2 or message.message_thread_id is None:
            return
        ticket_id = parts[1]
        token: str | None = None
        try:
            target = await self.ticket_service.get_ticket(ticket_id)
            async with self._ticket_locks.hold(target.telegram_user_id):
                # /bindtopic is an explicit full-admin recovery decision. Cancel
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
