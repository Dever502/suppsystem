from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import cast

from sqlalchemy import and_, case, exists, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import aliased

from resolvate.database import Database
from resolvate.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    NotificationOutbox,
    NotificationStatus,
    utcnow,
)
from resolvate.service_types import DeliveryJob, NotificationJob


class OutboxRepository:
    """Persistence and claim state machines for delivery and notification queues."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def claim_due_notifications(self, limit: int = 20) -> list[NotificationJob]:
        now = utcnow()
        claim_token = str(uuid.uuid4())
        candidate = aliased(NotificationOutbox)
        earlier = aliased(NotificationOutbox)
        unfinished_statuses = (
            NotificationStatus.PENDING,
            NotificationStatus.PROCESSING,
        )
        has_earlier_unfinished = exists(
            select(earlier.id).where(
                earlier.ticket_id == candidate.ticket_id,
                earlier.status.in_(unfinished_statuses),
                or_(
                    earlier.created_at < candidate.created_at,
                    and_(
                        earlier.created_at == candidate.created_at,
                        earlier.id < candidate.id,
                    ),
                ),
            )
        )
        claimable_ids = (
            select(candidate.id)
            .where(
                candidate.status == NotificationStatus.PENDING,
                candidate.next_attempt_at <= now,
                ~has_earlier_unfinished,
            )
            .order_by(candidate.created_at, candidate.id)
            .limit(limit)
        )
        async with self.database.session() as session:
            result = await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id.in_(claimable_ids),
                    NotificationOutbox.status == NotificationStatus.PENDING,
                    NotificationOutbox.next_attempt_at <= now,
                )
                .values(
                    status=NotificationStatus.PROCESSING,
                    claimed_at=now,
                    claim_token=claim_token,
                    attempt_count=NotificationOutbox.attempt_count + 1,
                )
                .returning(
                    NotificationOutbox.id,
                    NotificationOutbox.ticket_id,
                    NotificationOutbox.event_type,
                    NotificationOutbox.destination,
                    NotificationOutbox.recipient_identity_provider,
                    NotificationOutbox.recipient_identity_value,
                    NotificationOutbox.payload,
                    NotificationOutbox.created_at,
                    NotificationOutbox.attempt_count,
                )
            )
            rows = result.all()
            await session.commit()
            return [
                NotificationJob(
                    id=row.id,
                    ticket_id=row.ticket_id,
                    event_type=row.event_type,
                    destination=row.destination,
                    recipient_identity_provider=row.recipient_identity_provider,
                    recipient_identity_value=row.recipient_identity_value,
                    payload=row.payload,
                    created_at=row.created_at,
                    attempt_count=row.attempt_count,
                    claim_token=claim_token,
                )
                for row in rows
            ]

    async def mark_notification_delivered(self, notification_id: str, *, claim_token: str) -> bool:
        return await self._transition_notification(
            notification_id,
            claim_token,
            {
                "status": NotificationStatus.DELIVERED,
                "delivered_at": utcnow(),
                "claimed_at": None,
                "claim_token": None,
                "last_error": None,
                "payload": {},
            },
        )

    async def _transition_notification(
        self,
        notification_id: str,
        claim_token: str,
        values: Mapping[str, object],
    ) -> bool:
        async with self.database.session() as session:
            result = await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == notification_id,
                    NotificationOutbox.status == NotificationStatus.PROCESSING,
                    NotificationOutbox.claim_token == claim_token,
                )
                .values(**values)
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount == 1

    async def mark_notification_retry(
        self,
        notification_id: str,
        *,
        claim_token: str,
        error: str,
        retry_after_seconds: float,
        max_attempts: int,
    ) -> bool:
        ownership = (
            NotificationOutbox.id == notification_id,
            NotificationOutbox.status == NotificationStatus.PROCESSING,
            NotificationOutbox.claim_token == claim_token,
        )
        async with self.database.session() as session:
            terminal = await session.execute(
                update(NotificationOutbox)
                .where(*ownership, NotificationOutbox.attempt_count >= max_attempts)
                .values(
                    status=NotificationStatus.FAILED,
                    last_error=error[:1000],
                    claimed_at=None,
                    claim_token=None,
                    payload={},
                )
            )
            if cast(CursorResult[object], terminal).rowcount == 1:
                await session.commit()
                return True
            pending = await session.execute(
                update(NotificationOutbox)
                .where(*ownership)
                .values(
                    status=NotificationStatus.PENDING,
                    next_attempt_at=utcnow() + timedelta(seconds=retry_after_seconds),
                    last_error=error[:1000],
                    claimed_at=None,
                    claim_token=None,
                )
            )
            await session.commit()
            return cast(CursorResult[object], pending).rowcount == 1

    async def mark_notification_failed(
        self, notification_id: str, *, claim_token: str, error: str
    ) -> bool:
        return await self._transition_notification(
            notification_id,
            claim_token,
            {
                "status": NotificationStatus.FAILED,
                "last_error": error[:1000],
                "claimed_at": None,
                "claim_token": None,
                "payload": {},
            },
        )

    async def release_stale_notifications(self, stale_after_seconds: int = 300) -> int:
        threshold = utcnow() - timedelta(seconds=stale_after_seconds)
        async with self.database.session() as session:
            result = await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.status == NotificationStatus.PROCESSING,
                    NotificationOutbox.claimed_at < threshold,
                )
                .values(
                    status=NotificationStatus.PENDING,
                    claimed_at=None,
                    claim_token=None,
                )
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount

    async def claim_due_deliveries(self, limit: int = 20) -> list[DeliveryJob]:
        now = utcnow()
        claim_token = str(uuid.uuid4())
        candidate = aliased(DeliveryOutbox)
        earlier = aliased(DeliveryOutbox)
        unfinished_statuses = (
            DeliveryStatus.WAITING_TOPIC,
            DeliveryStatus.PENDING,
            DeliveryStatus.PROCESSING,
        )
        has_earlier_unfinished = exists(
            select(earlier.id).where(
                earlier.ticket_id == candidate.ticket_id,
                earlier.status.in_(unfinished_statuses),
                or_(
                    earlier.created_at < candidate.created_at,
                    and_(
                        earlier.created_at == candidate.created_at,
                        earlier.id < candidate.id,
                    ),
                ),
            )
        )
        claimable_ids = (
            select(candidate.id)
            .where(
                candidate.status == DeliveryStatus.PENDING,
                candidate.next_attempt_at <= now,
                ~has_earlier_unfinished,
            )
            .order_by(candidate.created_at, candidate.id)
            .limit(limit)
        )
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryOutbox)
                .where(
                    DeliveryOutbox.id.in_(claimable_ids),
                    DeliveryOutbox.status == DeliveryStatus.PENDING,
                    DeliveryOutbox.next_attempt_at <= now,
                )
                .values(
                    status=DeliveryStatus.PROCESSING,
                    claimed_at=now,
                    claim_token=claim_token,
                    attempt_count=DeliveryOutbox.attempt_count + 1,
                )
                .returning(
                    DeliveryOutbox.id,
                    DeliveryOutbox.ticket_id,
                    DeliveryOutbox.direction,
                    DeliveryOutbox.payload,
                    DeliveryOutbox.attempt_count,
                )
            )
            rows = result.all()
            await session.commit()
            return [
                DeliveryJob(
                    id=row.id,
                    ticket_id=row.ticket_id,
                    direction=Direction(row.direction),
                    payload=row.payload,
                    attempt_count=row.attempt_count,
                    claim_token=claim_token,
                )
                for row in rows
            ]

    async def mark_delivery_delivered(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        delivered_message_id: int | None = None,
    ) -> bool:
        return await self._transition_delivery(
            delivery_id,
            claim_token,
            {
                "status": DeliveryStatus.DELIVERED,
                "claimed_at": None,
                "claim_token": None,
                "delivered_at": utcnow(),
                "delivered_message_id": delivered_message_id,
                "last_error": None,
                "payload": {},
            },
        )

    async def mark_reopened_context_prepared(self, delivery_id: str, *, claim_token: str) -> bool:
        async with self.database.session() as session:
            delivery = await session.scalar(
                select(DeliveryOutbox)
                .where(
                    DeliveryOutbox.id == delivery_id,
                    DeliveryOutbox.status == DeliveryStatus.PROCESSING,
                    DeliveryOutbox.claim_token == claim_token,
                )
                .with_for_update()
            )
            if delivery is None:
                return False
            payload = dict(delivery.payload)
            payload.pop("prepare_reopened_context", None)
            delivery.payload = payload
            await session.commit()
            return True

    async def _transition_delivery(
        self,
        delivery_id: str,
        claim_token: str,
        values: Mapping[str, object],
    ) -> bool:
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryOutbox)
                .where(
                    DeliveryOutbox.id == delivery_id,
                    DeliveryOutbox.status == DeliveryStatus.PROCESSING,
                    DeliveryOutbox.claim_token == claim_token,
                )
                .values(**values)
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount == 1

    async def mark_delivery_cancelled(
        self, delivery_id: str, *, claim_token: str, reason: str
    ) -> bool:
        return await self._transition_delivery(
            delivery_id,
            claim_token,
            {
                "status": DeliveryStatus.CANCELLED,
                "claimed_at": None,
                "claim_token": None,
                "last_error": reason[:1000],
                "payload": {},
            },
        )

    async def mark_delivery_retry(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        error: str,
        retry_after_seconds: float,
        max_attempts: int,
    ) -> bool:
        ownership = (
            DeliveryOutbox.id == delivery_id,
            DeliveryOutbox.status == DeliveryStatus.PROCESSING,
            DeliveryOutbox.claim_token == claim_token,
        )
        async with self.database.session() as session:
            terminal = await session.execute(
                update(DeliveryOutbox)
                .where(*ownership, DeliveryOutbox.attempt_count >= max_attempts)
                .values(
                    status=DeliveryStatus.FAILED,
                    next_attempt_at=utcnow() + timedelta(seconds=retry_after_seconds),
                    claimed_at=None,
                    claim_token=None,
                    last_error=error[:1000],
                    payload={},
                )
            )
            if cast(CursorResult[object], terminal).rowcount == 1:
                await session.commit()
                return True
            pending = await session.execute(
                update(DeliveryOutbox)
                .where(*ownership)
                .values(
                    status=DeliveryStatus.PENDING,
                    next_attempt_at=utcnow() + timedelta(seconds=retry_after_seconds),
                    claimed_at=None,
                    claim_token=None,
                    last_error=error[:1000],
                )
            )
            await session.commit()
            return cast(CursorResult[object], pending).rowcount == 1

    async def release_stale_deliveries(self, stale_after_seconds: int = 300) -> int:
        threshold = utcnow() - timedelta(seconds=stale_after_seconds)
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryOutbox)
                .where(
                    DeliveryOutbox.status == DeliveryStatus.PROCESSING,
                    DeliveryOutbox.claimed_at < threshold,
                )
                .values(
                    status=DeliveryStatus.PENDING,
                    claimed_at=None,
                    claim_token=None,
                )
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount

    async def release_delivery_claims(self, claims: Sequence[tuple[str, str]]) -> int:
        """Requeue owned claims that a stopping worker has not started."""

        if not claims:
            return 0
        owned_claims = or_(
            *(
                and_(
                    DeliveryOutbox.id == delivery_id,
                    DeliveryOutbox.claim_token == claim_token,
                )
                for delivery_id, claim_token in claims
            )
        )
        async with self.database.session() as session:
            result = await session.execute(
                update(DeliveryOutbox)
                .where(
                    DeliveryOutbox.status == DeliveryStatus.PROCESSING,
                    owned_claims,
                )
                .values(
                    status=DeliveryStatus.PENDING,
                    attempt_count=case(
                        (DeliveryOutbox.attempt_count > 0, DeliveryOutbox.attempt_count - 1),
                        else_=0,
                    ),
                    claimed_at=None,
                    claim_token=None,
                )
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount
