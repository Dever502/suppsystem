from __future__ import annotations

import time
from collections import Counter, defaultdict
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
from suppsystem.runtime_health import RuntimeHealth


class MetricsRegistry:
    """Small process-local metrics registry with bounded, code-controlled labels."""

    def __init__(self) -> None:
        self._events: Counter[tuple[str, str]] = Counter()
        self._latency_count: Counter[tuple[str, str]] = Counter()
        self._latency_sum: dict[tuple[str, str], float] = defaultdict(float)

    def event(self, component: str, outcome: str) -> None:
        self._events[(component, outcome)] += 1

    def observe_request(self, component: str, outcome: str, duration_seconds: float) -> None:
        key = (component, outcome)
        self._latency_count[key] += 1
        self._latency_sum[key] += duration_seconds
        if outcome in {"http_5xx", "request_error"}:
            self.event(component, outcome)

    async def render(self, database: Database, runtime_health: RuntimeHealth) -> str:
        now = datetime.now(UTC)
        async with database.session() as session:
            delivery_depth, delivery_oldest = (
                await session.execute(
                    select(func.count(), func.min(DeliveryOutbox.created_at)).where(
                        DeliveryOutbox.status.in_(
                            (
                                DeliveryStatus.WAITING_TOPIC,
                                DeliveryStatus.PENDING,
                                DeliveryStatus.PROCESSING,
                            )
                        )
                    )
                )
            ).one()
            notification_depth, notification_oldest = (
                await session.execute(
                    select(func.count(), func.min(NotificationOutbox.created_at)).where(
                        NotificationOutbox.status.in_(
                            (
                                NotificationStatus.AWAITING_PAYLOAD,
                                NotificationStatus.PENDING,
                                NotificationStatus.PROCESSING,
                            )
                        )
                    )
                )
            ).one()
            delivery_failed = await session.scalar(
                select(func.count()).where(DeliveryOutbox.status == DeliveryStatus.FAILED)
            )
            notification_failed = await session.scalar(
                select(func.count()).where(NotificationOutbox.status == NotificationStatus.FAILED)
            )
            panel_unknown = await session.scalar(
                select(func.count()).where(
                    OperatorAction.action.like("remnawave_%"),
                    OperatorAction.result == "unknown",
                )
            )
            delivery_attempts = await session.scalar(select(func.sum(DeliveryOutbox.attempt_count)))
            notification_attempts = await session.scalar(
                select(func.sum(NotificationOutbox.attempt_count))
            )

        lines = [
            "# TYPE suppsystem_queue_depth gauge",
            f'suppsystem_queue_depth{{queue="delivery"}} {int(delivery_depth or 0)}',
            f'suppsystem_queue_depth{{queue="notification"}} {int(notification_depth or 0)}',
            "# TYPE suppsystem_queue_oldest_age_seconds gauge",
            'suppsystem_queue_oldest_age_seconds{queue="delivery"} '
            f"{self._age(now, delivery_oldest)}",
            'suppsystem_queue_oldest_age_seconds{queue="notification"} '
            f"{self._age(now, notification_oldest)}",
            "# TYPE suppsystem_failed_jobs gauge",
            f'suppsystem_failed_jobs{{queue="delivery"}} {int(delivery_failed or 0)}',
            f'suppsystem_failed_jobs{{queue="notification"}} {int(notification_failed or 0)}',
            "# TYPE suppsystem_retained_job_attempts gauge",
            f'suppsystem_retained_job_attempts{{queue="delivery"}} {int(delivery_attempts or 0)}',
            f'suppsystem_retained_job_attempts{{queue="notification"}} '
            f"{int(notification_attempts or 0)}",
            "# TYPE suppsystem_panel_unknown gauge",
            f"suppsystem_panel_unknown {int(panel_unknown or 0)}",
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
