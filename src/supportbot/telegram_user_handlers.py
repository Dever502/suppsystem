from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from supportbot.service_types import (
    TicketNotFoundError,
)
from supportbot.telegram_constants import SUPPORT_PENDING_TEXT, WELCOME_TEXT
from supportbot.telegram_message_utils import (
    media_metadata,
    message_text,
    rated_ticket_closed_text,
    rating_report,
)
from supportbot.telegram_topic_manager import TelegramTopicManager

logger = logging.getLogger(__name__)


class TelegramUserHandlers(TelegramTopicManager):
    async def handle_start(self, message: Message) -> None:
        logger.info(
            "Handled start command",
            extra={
                "event": "start_command_handled",
                "telegram_user_id": message.from_user.id if message.from_user else None,
                "chat_id": message.chat.id,
                "message_id": message.message_id,
            },
        )
        await message.answer(WELCOME_TEXT)

    async def handle_rating_callback(self, callback: CallbackQuery) -> None:
        try:
            if callback.data is None:
                raise ValueError
            _, ticket_id, close_cycle_raw, score_raw = callback.data.split(":", maxsplit=3)
            close_cycle = int(close_cycle_raw)
            score = int(score_raw)
            if score not in range(1, 6):
                raise ValueError
        except ValueError:
            await callback.answer("Некорректная оценка.", show_alert=False)
            return

        try:
            ticket = await self.ticket_service.get_ticket(ticket_id)
        except TicketNotFoundError:
            await callback.answer("Тикет не найден.", show_alert=True)
            return
        if callback.from_user.id != ticket.telegram_user_id:
            await callback.answer("Оценить обращение может только клиент.", show_alert=True)
            return

        queued = await self.ticket_service.enqueue_rating(
            ticket_id=ticket.id,
            source_chat_id=callback.from_user.id,
            score=score,
            close_cycle=close_cycle,
            target_chat_id=self.settings.support_group_id,
            text=rating_report(ticket, score),
            idempotency_key=(f"rating:{ticket.id}:{close_cycle}:{ticket.telegram_user_id}"),
            parse_mode="HTML",
        )
        logger.info(
            "Received support rating",
            extra={
                "event": "support_rating_received" if queued else "support_rating_duplicate",
                "ticket_id": ticket.id,
                "telegram_user_id": ticket.telegram_user_id,
                "chat_id": callback.from_user.id,
                "rating": score,
            },
        )
        if queued and isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    rated_ticket_closed_text(score),
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except TelegramAPIError:
                logger.exception(
                    "Unable to update closed ticket message with rating",
                    extra={
                        "event": "rating_confirmation_edit_failed",
                        "ticket_id": ticket.id,
                        "telegram_user_id": ticket.telegram_user_id,
                        "rating": score,
                    },
                )
        await callback.answer(
            "✅ Спасибо за оценку!" if queued else "ℹ️ Оценка уже принята.",
            show_alert=False,
        )

    async def handle_private_message(self, message: Message) -> None:
        if message.text and message.text.startswith("/"):
            await message.answer(
                "ℹ️ Отправьте обычное сообщение для поддержки или используйте /start."
            )
            return
        if message.from_user is None:
            return
        logger.info(
            "Received private support message",
            extra={
                "event": "private_message_received",
                "telegram_user_id": message.from_user.id,
                "chat_id": message.chat.id,
                "message_id": message.message_id,
            },
        )
        async with self._ticket_locks.hold(message.from_user.id):
            result = await self.ticket_service.accept_customer_message(
                telegram_user_id=message.from_user.id,
                display_name=message.from_user.full_name,
                username=message.from_user.username,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                target_chat_id=self.settings.support_group_id,
                content=message_text(message),
                media=media_metadata(message),
            )
            if result.blocked:
                logger.info(
                    "Ignored message from blocked user",
                    extra={
                        "event": "blocked_user_message_ignored",
                        "telegram_user_id": message.from_user.id,
                        "chat_id": message.chat.id,
                        "message_id": message.message_id,
                    },
                )
                return
            ticket = result.ticket
            if ticket is None:
                raise RuntimeError("accepted customer message has no ticket")
            logger.info(
                "Opened or restored ticket",
                extra={
                    "event": "ticket_opened_or_restored",
                    "ticket_id": ticket.id,
                    "telegram_user_id": ticket.telegram_user_id,
                    "topic_id": ticket.topic_id,
                },
            )
            queued = result.changed
            logger.info(
                "Persisted user message for delivery"
                if queued
                else "Duplicate user message ignored",
                extra={
                    "event": "user_message_queued" if queued else "user_message_duplicate",
                    "ticket_id": ticket.id,
                    "telegram_user_id": ticket.telegram_user_id,
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "topic_id": ticket.topic_id,
                },
            )
            try:
                ticket = await self._ensure_topic(ticket)
            except TelegramAPIError:
                await message.answer(SUPPORT_PENDING_TEXT)
                return
            except Exception:
                logger.exception(
                    "Unable to prepare support topic",
                    extra={
                        "event": "topic_provisioning_failed",
                        "ticket_id": ticket.id,
                        "telegram_user_id": ticket.telegram_user_id,
                        "topic_id": ticket.topic_id,
                    },
                )
                await message.answer(SUPPORT_PENDING_TEXT)
                return
            if ticket.topic_id is None:
                logger.warning(
                    "Ticket has no topic after provisioning",
                    extra={
                        "event": "topic_provisioning_incomplete",
                        "ticket_id": ticket.id,
                        "telegram_user_id": ticket.telegram_user_id,
                    },
                )
                await message.answer(SUPPORT_PENDING_TEXT)
                return
