from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.statistics import STATISTICS_PERIODS, StatisticsService, SupportStatistics
from suppsystem.telegram_limits import TelegramRateLimiter

logger = logging.getLogger(__name__)


def _dashboard_message_missing(error: TelegramBadRequest) -> bool:
    message = str(error).casefold()
    return any(
        fragment in message
        for fragment in (
            "message to edit not found",
            "message can't be edited",
            "message_id_invalid",
        )
    )


STATISTICS_CALLBACK_PREFIX = "suppsystem_stats"
PERIOD_LABELS = {"today": "Сегодня", "7d": "7 дней", "30d": "30 дней"}


def statistics_keyboard(period: str) -> InlineKeyboardMarkup:
    def button(value: str) -> InlineKeyboardButton:
        selected = " ·" if value == period else ""
        return InlineKeyboardButton(
            text=f"{PERIOD_LABELS[value]}{selected}",
            callback_data=f"{STATISTICS_CALLBACK_PREFIX}:{value}",
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data=f"{STATISTICS_CALLBACK_PREFIX}:{period}",
                )
            ],
            [button("today"), button("7d"), button("30d")],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=f"{STATISTICS_CALLBACK_PREFIX}:refresh:{period}",
                )
            ],
        ]
    )


def _format_moscow(value: datetime) -> str:
    from suppsystem.statistics import MOSCOW

    return value.astimezone(MOSCOW).strftime("%d.%m.%Y, %H:%M")


def statistics_text(statistics: SupportStatistics) -> str:
    rating = (
        f"{statistics.average_rating:.2f} / 5 ({statistics.rating_count})"
        if statistics.average_rating is not None
        else "нет оценок"
    )
    return (
        "📊 <b>Статистика поддержки</b>\n\n"
        f"Период: <b>{PERIOD_LABELS[statistics.period]}</b>\n"
        f"Обратились: <b>{statistics.contacted}</b>\n"
        f"├ Telegram: <b>{statistics.telegram_contacted}</b>\n"
        f"└ Web: <b>{statistics.web_contacted}</b>\n"
        f"Входящих сообщений: <b>{statistics.inbound_messages}</b>\n"
        f"Закрытий: <b>{statistics.closed}</b>\n"
        f"Активных сейчас: <b>{statistics.active}</b>\n"
        f"Средняя оценка: <b>{rating}</b>\n\n"
        f"Обновлено: {_format_moscow(statistics.generated_at)} MSK"
    )


class TelegramStatisticsDashboard:
    bot: Bot
    settings: Settings
    authorization: AuthorizationService
    limiter: TelegramRateLimiter
    statistics_service: StatisticsService

    async def ensure_statistics_dashboard(self) -> None:
        state = await self.statistics_service.dashboard_state()
        period = state.period if state.period in STATISTICS_PERIODS else "today"
        try:
            await self._render_statistics(period=period, refresh=True, message_id=state.message_id)
        except TelegramAPIError:
            logger.warning(
                "Unable to initialize statistics dashboard",
                exc_info=True,
                extra={"event": "statistics_dashboard_startup_degraded"},
            )

    async def _render_statistics(
        self, *, period: str, refresh: bool, message_id: int | None
    ) -> None:
        statistics = await self.statistics_service.get(period, refresh=refresh)
        text = statistics_text(statistics)
        keyboard = statistics_keyboard(period)
        if message_id is not None:
            try:
                await self.limiter.wait()
                await self.bot.edit_message_text(
                    chat_id=self.settings.support_group_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=keyboard,
                )
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).casefold():
                    await self.statistics_service.save_dashboard(
                        message_id=message_id, period=period
                    )
                    return
                if not _dashboard_message_missing(error):
                    raise
                logger.warning(
                    "Unable to update statistics dashboard; recreating it",
                    exc_info=True,
                    extra={"event": "statistics_dashboard_recreate", "message_id": message_id},
                )
            else:
                await self.statistics_service.save_dashboard(message_id=message_id, period=period)
                return
        await self.limiter.wait()
        message = await self.bot.send_message(
            chat_id=self.settings.support_group_id,
            text=text,
            reply_markup=keyboard,
        )
        await self.statistics_service.save_dashboard(message_id=message.message_id, period=period)

    async def handle_statistics_callback(self, callback: CallbackQuery) -> None:
        if callback.from_user is None or not self.authorization.is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        data = callback.data or ""
        parts = data.split(":")
        refresh = len(parts) == 3 and parts[1] == "refresh"
        period = parts[2] if refresh else parts[1] if len(parts) == 2 else ""
        if period not in STATISTICS_PERIODS:
            await callback.answer("Некорректный период.", show_alert=False)
            return
        state = await self.statistics_service.dashboard_state()
        await self._render_statistics(
            period=period,
            refresh=refresh,
            message_id=state.message_id,
        )
        await callback.answer("Статистика обновлена.", show_alert=False)
