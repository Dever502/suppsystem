from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, select

from resolvate.database import Database
from resolvate.models import Direction, Ticket, TicketChannel, TicketMessage, TicketStatus
from resolvate.web_models import OperatorDashboardState, TicketLifecycleEvent

MOSCOW = ZoneInfo("Europe/Moscow")
STATISTICS_PERIODS = frozenset({"today", "7d", "30d"})


@dataclass(frozen=True)
class SupportStatistics:
    period: str
    started_at: datetime
    generated_at: datetime
    contacted: int
    telegram_contacted: int
    web_contacted: int
    inbound_messages: int
    closed: int
    active: int
    average_rating: float | None
    rating_count: int


def period_start(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    if period not in STATISTICS_PERIODS:
        raise ValueError("unsupported statistics period")
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    generated_at = generated_at.astimezone(UTC)
    local_now = generated_at.astimezone(MOSCOW)
    days = {"today": 1, "7d": 7, "30d": 30}[period]
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    return local_start.astimezone(UTC), generated_at


class StatisticsService:
    def __init__(self, database: Database, *, cache_ttl_seconds: float = 10.0) -> None:
        self.database = database
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, SupportStatistics]] = {}
        self._lock = asyncio.Lock()

    async def get(self, period: str, *, refresh: bool = False) -> SupportStatistics:
        if period not in STATISTICS_PERIODS:
            raise ValueError("unsupported statistics period")
        cached = self._cache.get(period)
        if (
            not refresh
            and cached is not None
            and time.monotonic() - cached[0] < self.cache_ttl_seconds
        ):
            return cached[1]
        async with self._lock:
            cached = self._cache.get(period)
            if (
                not refresh
                and cached is not None
                and time.monotonic() - cached[0] < self.cache_ttl_seconds
            ):
                return cached[1]
            statistics = await self._query(period)
            self._cache[period] = (time.monotonic(), statistics)
            return statistics

    async def _query(self, period: str) -> SupportStatistics:
        started_at, generated_at = period_start(period)
        customer_filter = (
            TicketMessage.direction == Direction.USER_TO_OPERATOR,
            TicketMessage.channel != "rating",
            TicketMessage.suppressed.is_(False),
            TicketMessage.created_at >= started_at,
            TicketMessage.created_at <= generated_at,
        )
        async with self.database.session() as session:
            contacted = int(
                await session.scalar(
                    select(func.count(distinct(TicketMessage.ticket_id))).where(*customer_filter)
                )
                or 0
            )
            channel_rows = (
                await session.execute(
                    select(Ticket.channel, func.count(distinct(TicketMessage.ticket_id)))
                    .join(TicketMessage, TicketMessage.ticket_id == Ticket.id)
                    .where(*customer_filter)
                    .group_by(Ticket.channel)
                )
            ).all()
            by_channel = {TicketChannel(channel): int(count) for channel, count in channel_rows}
            inbound_messages = int(
                await session.scalar(
                    select(func.count()).select_from(TicketMessage).where(*customer_filter)
                )
                or 0
            )
            closed = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TicketLifecycleEvent)
                    .where(
                        TicketLifecycleEvent.event_type == "closed",
                        TicketLifecycleEvent.created_at >= started_at,
                        TicketLifecycleEvent.created_at <= generated_at,
                    )
                )
                or 0
            )
            active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Ticket)
                    .where(Ticket.status != TicketStatus.CLOSED)
                )
                or 0
            )
            rating_media = list(
                (
                    await session.scalars(
                        select(TicketMessage.media).where(
                            TicketMessage.channel == "rating",
                            TicketMessage.created_at >= started_at,
                            TicketMessage.created_at <= generated_at,
                            TicketMessage.suppressed.is_(False),
                        )
                    )
                ).all()
            )
        ratings = [
            float(media["rating"])
            for media in rating_media
            if isinstance(media, dict) and isinstance(media.get("rating"), (int, float))
        ]
        return SupportStatistics(
            period=period,
            started_at=started_at,
            generated_at=generated_at,
            contacted=contacted,
            telegram_contacted=by_channel.get(TicketChannel.TELEGRAM, 0),
            web_contacted=by_channel.get(TicketChannel.WEB, 0),
            inbound_messages=inbound_messages,
            closed=closed,
            active=active,
            average_rating=sum(ratings) / len(ratings) if ratings else None,
            rating_count=len(ratings),
        )

    async def dashboard_state(self) -> OperatorDashboardState:
        async with self.database.session() as session:
            state = await session.get(OperatorDashboardState, 1)
            if state is None:
                state = OperatorDashboardState(id=1, period="today")
                session.add(state)
                await session.commit()
            return state

    async def save_dashboard(self, *, message_id: int, period: str) -> None:
        if period not in STATISTICS_PERIODS:
            raise ValueError("unsupported statistics period")
        async with self.database.session() as session:
            state = await session.get(OperatorDashboardState, 1)
            if state is None:
                session.add(OperatorDashboardState(id=1, message_id=message_id, period=period))
            else:
                state.message_id = message_id
                state.period = period
            await session.commit()
