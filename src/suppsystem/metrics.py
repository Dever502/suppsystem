from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from suppsystem.database import Database
from suppsystem.models import (
    DeliveryOutbox,
    DeliveryStatus,
    NotificationOutbox,
    NotificationStatus,
    OperatorAction,
)
from suppsystem.runtime_defaults import METRICS_DATABASE_CACHE_TTL_SECONDS
from suppsystem.runtime_health import RuntimeHealth


@dataclass(frozen=True)
class _DatabaseMetrics:
    delivery_depth: int
    delivery_oldest: datetime | None
    delivery_failed: int
    delivery_attempts: int
    notification_depth: int
    notification_oldest: datetime | None
    notification_failed: int
    notification_attempts: int
    panel_unknown: int


class MetricsRegistry:
    """Small process-local metrics registry with bounded, code-controlled labels."""

    def __init__(
        self,
        *,
        database_cache_ttl_seconds: float = METRICS_DATABASE_CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if database_cache_ttl_seconds <= 0:
            raise ValueError("database_cache_ttl_seconds must be positive")
        self._events: Counter[tuple[str, str]] = Counter()
        self._latency_count: Counter[tuple[str, str]] = Counter()
        self._latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._database_cache_ttl_seconds = database_cache_ttl_seconds
        self._monotonic = monotonic
        self._database_cache: tuple[float, _DatabaseMetrics] | None = None
        self._database_cache_lock = asyncio.Lock()

    def event(self, component: str, outcome: str) -> None:
        self._events[(component, outcome)] += 1

    def observe_request(self, component: str, outcome: str, duration_seconds: float) -> None:
        key = (component, outcome)
        self._latency_count[key] += 1
        self._latency_sum[key] += duration_seconds
        if outcome in {"http_5xx", "request_error"}:
            self.event(component, outcome)

    async def _database_metrics(self, database: Database) -> _DatabaseMetrics:
        now = self._monotonic()
        cached = self._database_cache
        if cached is not None and now - cached[0] < self._database_cache_ttl_seconds:
            return cached[1]
        async with self._database_cache_lock:
            now = self._monotonic()
            cached = self._database_cache
            if cached is not None and now - cached[0] < self._database_cache_ttl_seconds:
                return cached[1]
            snapshot = await self._query_database_metrics(database)
            self._database_cache = (now, snapshot)
            return snapshot

    @staticmethod
    async def _query_database_metrics(database: Database) -> _DatabaseMetrics:
        delivery_active = DeliveryOutbox.status.in_(
            (
                DeliveryStatus.WAITING_TOPIC,
                DeliveryStatus.PENDING,
                DeliveryStatus.PROCESSING,
            )
        )
        notification_active = NotificationOutbox.status.in_(
            (
                NotificationStatus.AWAITING_PAYLOAD,
                NotificationStatus.PENDING,
                NotificationStatus.PROCESSING,
            )
        )
        async with database.session() as session:
            delivery = (
                await session.execute(
                    select(
                        func.count().filter(delivery_active),
                        func.min(DeliveryOutbox.created_at).filter(delivery_active),
                        func.count().filter(DeliveryOutbox.status == DeliveryStatus.FAILED),
                        func.sum(DeliveryOutbox.attempt_count),
                    )
                )
            ).one()
            notification = (
                await session.execute(
                    select(
                        func.count().filter(notification_active),
                        func.min(NotificationOutbox.created_at).filter(notification_active),
                        func.count().filter(NotificationOutbox.status == NotificationStatus.FAILED),
                        func.sum(NotificationOutbox.attempt_count),
                    )
                )
            ).one()
            panel_unknown = await session.scalar(
                select(func.count()).where(
                    OperatorAction.action.like("remnawave_%"),
                    OperatorAction.result == "unknown",
                )
            )
        return _DatabaseMetrics(
            delivery_depth=int(delivery[0] or 0),
            delivery_oldest=delivery[1],
            delivery_failed=int(delivery[2] or 0),
            delivery_attempts=int(delivery[3] or 0),
            notification_depth=int(notification[0] or 0),
            notification_oldest=notification[1],
            notification_failed=int(notification[2] or 0),
            notification_attempts=int(notification[3] or 0),
            panel_unknown=int(panel_unknown or 0),
        )

    async def render(self, database: Database, runtime_health: RuntimeHealth) -> str:
        now = datetime.now(UTC)
        snapshot = await self._database_metrics(database)
        lines = [
            "# TYPE suppsystem_queue_depth gauge",
            f'suppsystem_queue_depth{{queue="delivery"}} {snapshot.delivery_depth}',
            f'suppsystem_queue_depth{{queue="notification"}} {snapshot.notification_depth}',
            "# TYPE suppsystem_queue_oldest_age_seconds gauge",
            'suppsystem_queue_oldest_age_seconds{queue="delivery"} '
            f"{self._age(now, snapshot.delivery_oldest)}",
            'suppsystem_queue_oldest_age_seconds{queue="notification"} '
            f"{self._age(now, snapshot.notification_oldest)}",
            "# TYPE suppsystem_failed_jobs gauge",
            f'suppsystem_failed_jobs{{queue="delivery"}} {snapshot.delivery_failed}',
            f'suppsystem_failed_jobs{{queue="notification"}} {snapshot.notification_failed}',
            "# TYPE suppsystem_retained_job_attempts gauge",
            f'suppsystem_retained_job_attempts{{queue="delivery"}} {snapshot.delivery_attempts}',
            'suppsystem_retained_job_attempts{queue="notification"} '
            f"{snapshot.notification_attempts}",
            "# TYPE suppsystem_panel_unknown gauge",
            f"suppsystem_panel_unknown {snapshot.panel_unknown}",
            "# TYPE suppsystem_events_total counter",
        ]
        for (component, outcome), value in sorted(self._events.items()):
            lines.append(
                f'suppsystem_events_total{{component="{component}",outcome="{outcome}"}} {value}'
            )
        lines.append("# TYPE suppsystem_external_request_duration_seconds summary")
        for component, outcome in sorted(self._latency_count):
            labels = f'component="{component}",outcome="{outcome}"'
            lines.append(
                "suppsystem_external_request_duration_seconds_count"
                f"{{{labels}}} {self._latency_count[(component, outcome)]}"
            )
            lines.append(
                "suppsystem_external_request_duration_seconds_sum"
                f"{{{labels}}} {self._latency_sum[(component, outcome)]:.6f}"
            )
        lines.append("# TYPE suppsystem_heartbeat_age_seconds gauge")
        for component, age in runtime_health.progress_ages(now=time.monotonic()).items():
            lines.append(f'suppsystem_heartbeat_age_seconds{{component="{component}"}} {age:.6f}')
        return "\n".join(lines) + "\n"

    @staticmethod
    def _age(now: datetime, created_at: datetime | None) -> str:
        if created_at is None:
            return "0"
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return f"{max(0.0, (now - created_at).total_seconds()):.6f}"
