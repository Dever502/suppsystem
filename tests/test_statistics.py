from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from pydantic import SecretStr

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.migrations import upgrade_database
from suppsystem.models import Direction, Ticket, TicketChannel, TicketMessage, TicketStatus, User
from suppsystem.statistics import StatisticsService, period_start
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.telegram_statistics import (
    StatisticsDashboardRefreshWorker,
    TelegramStatisticsDashboard,
    statistics_keyboard,
    statistics_text,
)
from suppsystem.web_models import TicketLifecycleEvent


async def _database(tmp_path: Path) -> Database:
    url = f"sqlite+aiosqlite:///{tmp_path}/statistics.db"
    await upgrade_database(url)
    return Database(url)


async def test_statistics_are_channel_aware_and_exclude_ratings_from_inbound(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    now = datetime.now(UTC)
    try:
        async with database.session() as session:
            telegram_user = User(display_name="Telegram", username="telegram")
            web_user = User(display_name="Web", email="web@example.com")
            session.add_all([telegram_user, web_user])
            await session.flush()
            telegram_ticket = Ticket(
                user_id=telegram_user.id,
                channel=TicketChannel.TELEGRAM,
                status=TicketStatus.OPEN,
                created_at=now,
                last_activity_at=now,
            )
            web_ticket = Ticket(
                user_id=web_user.id,
                channel=TicketChannel.WEB,
                status=TicketStatus.CLOSED,
                created_at=now,
                last_activity_at=now,
                closed_at=now,
                close_cycle=1,
            )
            session.add_all([telegram_ticket, web_ticket])
            await session.flush()
            session.add_all(
                [
                    TicketMessage(
                        ticket_id=telegram_ticket.id,
                        direction=Direction.USER_TO_OPERATOR,
                        channel="telegram",
                        content="one",
                        created_at=now,
                    ),
                    TicketMessage(
                        ticket_id=telegram_ticket.id,
                        direction=Direction.USER_TO_OPERATOR,
                        channel="telegram",
                        content="two",
                        created_at=now,
                    ),
                    TicketMessage(
                        ticket_id=web_ticket.id,
                        direction=Direction.USER_TO_OPERATOR,
                        channel="web",
                        content="web",
                        created_at=now,
                    ),
                    TicketMessage(
                        ticket_id=web_ticket.id,
                        direction=Direction.USER_TO_OPERATOR,
                        channel="web",
                        content="silently suppressed",
                        suppressed=True,
                        created_at=now,
                    ),
                    TicketMessage(
                        ticket_id=web_ticket.id,
                        direction=Direction.USER_TO_OPERATOR,
                        channel="rating",
                        content="4/5",
                        media={"rating": 4},
                        rating_cycle=1,
                        created_at=now,
                    ),
                    TicketMessage(
                        ticket_id=web_ticket.id,
                        direction=Direction.USER_TO_OPERATOR,
                        channel="rating",
                        content="1/5",
                        media={"rating": 1},
                        rating_cycle=2,
                        suppressed=True,
                        created_at=now,
                    ),
                    TicketLifecycleEvent(
                        ticket_id=web_ticket.id,
                        event_type="closed",
                        channel=TicketChannel.WEB,
                        close_cycle=1,
                        created_at=now,
                    ),
                ]
            )
            await session.commit()

        result = await StatisticsService(database).get("today")
        assert result.contacted == 2
        assert result.telegram_contacted == 1
        assert result.web_contacted == 1
        assert result.inbound_messages == 3
        assert result.closed == 1
        assert result.active == 1
        assert result.average_rating == 4
        assert result.rating_count == 1
        assert "Telegram: <b>1</b>" in statistics_text(result)
        keyboard = statistics_keyboard("today")
        assert keyboard.inline_keyboard[0][0].text == "📊 Статистика"
        assert keyboard.inline_keyboard[1][0].text.startswith("Сегодня")
        assert len(keyboard.inline_keyboard) == 2
        assert all(
            button.text != "🔄 Обновить" for row in keyboard.inline_keyboard for button in row
        )
    finally:
        await database.dispose()


async def test_statistics_dashboard_refresh_worker_updates_and_stops() -> None:
    refreshed = asyncio.Event()

    async def ensure_dashboard() -> None:
        refreshed.set()

    dashboard = SimpleNamespace(ensure_statistics_dashboard=ensure_dashboard)
    worker = StatisticsDashboardRefreshWorker(dashboard, interval_seconds=0.01)  # type: ignore[arg-type]
    task = asyncio.create_task(worker.run())

    await asyncio.wait_for(refreshed.wait(), timeout=1)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)


def test_statistics_periods_use_moscow_calendar_boundaries() -> None:
    now = datetime(2026, 7, 31, 21, 30, tzinfo=UTC)
    today, generated = period_start("today", now)
    seven_days, _ = period_start("7d", now)
    assert generated == now
    assert today == datetime(2026, 7, 31, 21, 0, tzinfo=UTC)
    assert seven_days == today - timedelta(days=6)


class DashboardHarness(TelegramStatisticsDashboard):
    pass


async def test_dashboard_persists_one_message_and_reuses_it(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)),
        edit_message_text=AsyncMock(),
    )
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        admin_telegram_ids={42},
    )
    dashboard = DashboardHarness()
    dashboard.bot = bot
    dashboard.settings = settings
    dashboard.authorization = AuthorizationService(settings)
    dashboard.limiter = TelegramRateLimiter(0)
    dashboard.statistics_service = StatisticsService(database)
    try:
        await dashboard.ensure_statistics_dashboard()
        state = await dashboard.statistics_service.dashboard_state()
        assert state.message_id == 501
        bot.send_message.assert_awaited_once()

        await dashboard.ensure_statistics_dashboard()
        bot.edit_message_text.assert_awaited_once()
        bot.send_message.assert_awaited_once()

        missing_method = EditMessageText(chat_id=-100123, message_id=501, text="test")
        bot.edit_message_text.side_effect = TelegramBadRequest(
            method=missing_method, message="Bad Request: message to edit not found"
        )
        bot.send_message.return_value = SimpleNamespace(message_id=502)
        await dashboard.ensure_statistics_dashboard()
        state = await dashboard.statistics_service.dashboard_state()
        assert state.message_id == 502
        assert bot.send_message.await_count == 2

        transient_method = EditMessageText(chat_id=-100123, message_id=502, text="test")
        bot.edit_message_text.side_effect = TelegramBadRequest(
            method=transient_method, message="Bad Request: temporary Telegram failure"
        )
        with pytest.raises(TelegramBadRequest):
            await dashboard._render_statistics(period="today", refresh=True, message_id=502)
        assert bot.send_message.await_count == 2

        await dashboard.ensure_statistics_dashboard()
        assert bot.send_message.await_count == 2

        unauthorized = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data="suppsystem_stats:today",
            answer=AsyncMock(),
        )
        await dashboard.handle_statistics_callback(unauthorized)
        unauthorized.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)
    finally:
        await database.dispose()
