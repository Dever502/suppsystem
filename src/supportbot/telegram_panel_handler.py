from __future__ import annotations

from aiogram.types import Message

from supportbot.panel import PanelService
from supportbot.service_types import TicketView
from supportbot.telegram_formatting import panel_action_reply

GIFT_DAYS_ERROR_TEXT = (
    "⚠️ <b>Неверное количество дней</b>\n\n"
    "Укажите целое число от 1 до 9999. Пример: <code>/gift 30</code>"
)


class TelegramPanelCommandHandler:
    def __init__(self, panel_service: PanelService | None) -> None:
        self.panel_service = panel_service

    async def handle(
        self,
        message: Message,
        ticket: TicketView,
        command: str,
        command_key: str,
        command_argument: str,
    ) -> None:
        if message.from_user is None:
            return
        if self.panel_service is None:
            await message.reply("⚠️ Интеграция с Remnawave не подключена.")
            return

        if command == "/gift":
            try:
                extend_days = int(command_argument)
            except ValueError:
                await message.reply(GIFT_DAYS_ERROR_TEXT)
                return
            if not 1 <= extend_days <= 9999:
                await message.reply(GIFT_DAYS_ERROR_TEXT)
                return
            result = await self.panel_service.extend_subscription_for_ticket(
                ticket=ticket,
                operator_telegram_id=message.from_user.id,
                extend_days=extend_days,
                idempotency_key=command_key,
            )
        elif command == "/resetkey":
            result = await self.panel_service.reset_key_for_ticket(
                ticket=ticket,
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
            )
        elif command == "/revokelink":
            result = await self.panel_service.revoke_subscription_link_for_ticket(
                ticket=ticket,
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
            )
        elif command == "/resetdevices":
            result = await self.panel_service.reset_devices_for_ticket(
                ticket=ticket,
                operator_telegram_id=message.from_user.id,
                idempotency_key=command_key,
            )
        else:
            return

        reply = panel_action_reply(result)
        if command in {"/gift", "/revokelink"} and result.completed:
            reply += "\n\n✅ Уведомление пользователю поставлено в очередь."
        await message.reply(reply)
