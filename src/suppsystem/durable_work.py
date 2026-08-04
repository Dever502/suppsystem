from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import and_, case, delete, exists, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from suppsystem.database import Database
from suppsystem.models import (
    DeliveryOutbox,
    DeliveryStatus,
    InboundUpdate,
    NotificationOutbox,
    NotificationStatus,
    OperatorAction,
    ReconciliationOutbox,
    TicketMessage,
    WorkStatus,
    utcnow,
)

MAX_RECONCILIATION_ATTEMPTS = 20


@dataclass(frozen=True)
class InboundUpdateJob:
    telegram_update_id: int
    payload: dict[str, object]
    attempt_count: int
    claim_token: str


@dataclass(frozen=True)
class ReconciliationJob:
    id: str
    kind: str
    ticket_id: str | None
    operator_action_id: str | None
    payload: dict[str, object]
    attempt_count: int
    claim_token: str


@dataclass(frozen=True)
class RetentionCleanupResult:
    inbound_updates: int
    delivery_outbox: int
    notification_outbox: int
    reconciliation_outbox: int
    ticket_messages_scrubbed: int = 0

    @property
    def total(self) -> int:
        return (
            self.inbound_updates
            + self.delivery_outbox
            + self.notification_outbox
            + self.reconciliation_outbox
            + self.ticket_messages_scrubbed
        )


async def enqueue_topic_reconciliation(
    session: AsyncSession,
    *,
    ticket_id: str,
    desired_status: str,
) -> None:
    await enqueue_topic_reconciliations(
        session,
        ticket_ids=(ticket_id,),
        payload={"desired_status": desired_status},
    )


async def enqueue_topic_reconciliations(
    session: AsyncSession,
    *,
    ticket_ids: Sequence[str],
    payload: Mapping[str, object],
    next_attempt_at: datetime | None = None,
) -> None:
    if not ticket_ids:
        return
    retry_at = utcnow() if next_attempt_at is None else next_attempt_at
    await session.execute(
        update(ReconciliationOutbox)
        .where(
            ReconciliationOutbox.kind == "telegram_topic",
            ReconciliationOutbox.ticket_id.in_(ticket_ids),
        )
        .values(
            payload=dict(payload),
            status=WorkStatus.PENDING,
            attempt_count=0,
            next_attempt_at=retry_at,
            claimed_at=None,
            claim_token=None,
            delivered_at=None,
            last_error=None,
        )
    )
    queued_ticket_ids = set(
        (
            await session.scalars(
                select(ReconciliationOutbox.ticket_id).where(
                    ReconciliationOutbox.kind == "telegram_topic",
                    ReconciliationOutbox.ticket_id.in_(ticket_ids),
                )
            )
        ).all()
    )
    session.add_all(
        ReconciliationOutbox(
            idempotency_key=f"topic:{ticket_id}",
            kind="telegram_topic",
            ticket_id=ticket_id,
            payload=dict(payload),
            next_attempt_at=retry_at,
        )
        for ticket_id in ticket_ids
        if ticket_id not in queued_ticket_ids
    )


async def enqueue_panel_reconciliation(
    session: AsyncSession,
    *,
    ticket_id: str,
    operator_action_id: str,
    payload: Mapping[str, object],
    delay_seconds: float,
) -> None:
    key = f"remnawave:{operator_action_id}"
    entry = await session.scalar(
        select(ReconciliationOutbox).where(ReconciliationOutbox.idempotency_key == key)
    )
    if entry is not None:
        return
    session.add(
        ReconciliationOutbox(
            idempotency_key=key,
            kind="remnawave",
            ticket_id=ticket_id,
            operator_action_id=operator_action_id,
            payload=dict(payload),
            next_attempt_at=utcnow() + timedelta(seconds=delay_seconds),
        )
    )


class DurableWorkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def enqueue_inbound_update(
        self,
        telegram_update_id: int,
        payload: dict[str, object],
        *,
        ordering_key: str,
    ) -> bool:
        if not ordering_key or len(ordering_key) > 64:
            raise ValueError("ordering_key must contain 1 to 64 characters")
        async with self.database.session() as session:
            if await session.get(InboundUpdate, telegram_update_id) is not None:
                return False
            session.add(
                InboundUpdate(
                    telegram_update_id=telegram_update_id,
                    ordering_key=ordering_key,
                    payload=payload,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.get(InboundUpdate, telegram_update_id) is not None:
                    return False
                raise
            return True

    async def claim_inbound_update(self) -> InboundUpdateJob | None:
        now = utcnow()
        token = str(uuid.uuid4())
        candidate = aliased(InboundUpdate)
        earlier = aliased(InboundUpdate)
        has_earlier_unfinished = exists(
            select(earlier.telegram_update_id).where(
                earlier.ordering_key == candidate.ordering_key,
                earlier.status.in_((WorkStatus.PENDING, WorkStatus.PROCESSING)),
                earlier.telegram_update_id < candidate.telegram_update_id,
            )
        ).correlate(candidate)
        claimable_id = (
            select(candidate.telegram_update_id)
            .where(
                candidate.status == WorkStatus.PENDING,
                candidate.next_attempt_at <= now,
                ~has_earlier_unfinished,
            )
            .order_by(candidate.telegram_update_id)
            .limit(1)
        )
        async with self.database.session() as session:
            result = await session.execute(
                update(InboundUpdate)
                .where(
                    InboundUpdate.telegram_update_id.in_(claimable_id),
                    InboundUpdate.status == WorkStatus.PENDING,
                    InboundUpdate.next_attempt_at <= now,
                )
                .values(
                    status=WorkStatus.PROCESSING,
                    claimed_at=now,
                    claim_token=token,
                    attempt_count=InboundUpdate.attempt_count + 1,
                )
                .returning(
                    InboundUpdate.telegram_update_id,
                    InboundUpdate.payload,
                    InboundUpdate.attempt_count,
                )
            )
            row = result.first()
            await session.commit()
            if row is None:
                return None
            return InboundUpdateJob(
                telegram_update_id=row.telegram_update_id,
                payload=row.payload,
                attempt_count=row.attempt_count,
                claim_token=token,
            )

    async def finish_inbound_update(self, job: InboundUpdateJob) -> bool:
        async with self.database.session() as session:
            result = await session.execute(
                update(InboundUpdate)
                .where(
                    InboundUpdate.telegram_update_id == job.telegram_update_id,
                    InboundUpdate.status == WorkStatus.PROCESSING,
                    InboundUpdate.claim_token == job.claim_token,
                )
                .values(
                    payload={},
                    status=WorkStatus.DELIVERED,
                    processed_at=utcnow(),
                    claimed_at=None,
                    claim_token=None,
                    last_error=None,
                )
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount == 1

    async def retry_inbound_update(
        self, job: InboundUpdateJob, error: str, *, max_attempts: int = 20
    ) -> bool:
        async with self.database.session() as session:
            result = await session.execute(
                update(InboundUpdate)
                .where(
                    InboundUpdate.telegram_update_id == job.telegram_update_id,
                    InboundUpdate.status == WorkStatus.PROCESSING,
                    InboundUpdate.claim_token == job.claim_token,
                )
                .values(
                    status=case(
                        (InboundUpdate.attempt_count >= max_attempts, WorkStatus.FAILED),
                        else_=WorkStatus.PENDING,
                    ),
                    next_attempt_at=utcnow()
                    + timedelta(seconds=min(300.0, 2.0 ** min(job.attempt_count, 8))),
                    claimed_at=None,
                    claim_token=None,
                    last_error=error[:1000],
                )
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount == 1

    async def release_stale_inbound_updates(self, stale_after_seconds: int = 300) -> int:
        threshold = utcnow() - timedelta(seconds=stale_after_seconds)
        async with self.database.session() as session:
            result = await session.execute(
                update(InboundUpdate)
                .where(
                    InboundUpdate.status == WorkStatus.PROCESSING,
                    InboundUpdate.claimed_at < threshold,
                )
                .values(status=WorkStatus.PENDING, claimed_at=None, claim_token=None)
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount

    async def purge_expired_terminal_work(
        self,
        *,
        now: datetime | None = None,
        inbound_retention: timedelta = timedelta(days=7),
        outbox_retention: timedelta = timedelta(days=30),
    ) -> RetentionCleanupResult:
        if inbound_retention <= timedelta(0) or outbox_retention <= timedelta(0):
            raise ValueError("retention periods must be positive")
        current_time = utcnow() if now is None else now
        inbound_before = current_time - inbound_retention
        outbox_before = current_time - outbox_retention
        async with self.database.session() as session:
            sensitive_messages = await session.execute(
                update(TicketMessage)
                .where(
                    TicketMessage.sensitive.is_(True),
                    TicketMessage.created_at < outbox_before,
                )
                .values(
                    content="🔒 Ссылка скрыта по истечении срока хранения.",
                    sensitive=False,
                )
            )
            await session.execute(
                update(DeliveryOutbox)
                .where(
                    DeliveryOutbox.status == DeliveryStatus.FAILED,
                    DeliveryOutbox.created_at < outbox_before,
                )
                .values(payload={})
            )
            await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.event_type == "subscription_link_reissued",
                    NotificationOutbox.status.in_(
                        (
                            NotificationStatus.AWAITING_PAYLOAD,
                            NotificationStatus.PENDING,
                            NotificationStatus.FAILED,
                        )
                    ),
                    NotificationOutbox.created_at < outbox_before,
                )
                .values(status=NotificationStatus.CANCELLED, payload={})
            )
            await session.execute(
                update(ReconciliationOutbox)
                .where(
                    ReconciliationOutbox.status == WorkStatus.FAILED,
                    ReconciliationOutbox.created_at < outbox_before,
                )
                .values(payload={})
            )
            inbound = await session.execute(
                delete(InboundUpdate).where(
                    InboundUpdate.status == WorkStatus.DELIVERED,
                    InboundUpdate.processed_at < inbound_before,
                )
            )
            deliveries = await session.execute(
                delete(DeliveryOutbox).where(
                    or_(
                        and_(
                            DeliveryOutbox.status == DeliveryStatus.DELIVERED,
                            DeliveryOutbox.delivered_at < outbox_before,
                        ),
                        and_(
                            DeliveryOutbox.status == DeliveryStatus.CANCELLED,
                            DeliveryOutbox.created_at < outbox_before,
                        ),
                    )
                )
            )
            notifications = await session.execute(
                delete(NotificationOutbox).where(
                    or_(
                        and_(
                            NotificationOutbox.status == NotificationStatus.DELIVERED,
                            NotificationOutbox.delivered_at < outbox_before,
                        ),
                        and_(
                            NotificationOutbox.status == NotificationStatus.CANCELLED,
                            NotificationOutbox.created_at < outbox_before,
                        ),
                    )
                )
            )
            reconciliations = await session.execute(
                delete(ReconciliationOutbox).where(
                    ReconciliationOutbox.status == WorkStatus.DELIVERED,
                    ReconciliationOutbox.delivered_at < outbox_before,
                )
            )
            await session.commit()
        return RetentionCleanupResult(
            inbound_updates=cast(CursorResult[object], inbound).rowcount,
            delivery_outbox=cast(CursorResult[object], deliveries).rowcount,
            notification_outbox=cast(CursorResult[object], notifications).rowcount,
            reconciliation_outbox=cast(CursorResult[object], reconciliations).rowcount,
            ticket_messages_scrubbed=cast(CursorResult[object], sensitive_messages).rowcount,
        )

    async def claim_reconciliations(self, limit: int) -> list[ReconciliationJob]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        now = utcnow()
        token = str(uuid.uuid4())
        candidate = aliased(ReconciliationOutbox)
        earlier = aliased(ReconciliationOutbox)
        has_earlier_unfinished = exists(
            select(earlier.id).where(
                earlier.ticket_id == candidate.ticket_id,
                earlier.status.in_((WorkStatus.PENDING, WorkStatus.PROCESSING)),
                or_(
                    earlier.created_at < candidate.created_at,
                    and_(
                        earlier.created_at == candidate.created_at,
                        earlier.id < candidate.id,
                    ),
                ),
            )
        ).correlate(candidate)
        claimable_ids = (
            select(candidate.id)
            .where(
                candidate.status == WorkStatus.PENDING,
                candidate.next_attempt_at <= now,
                or_(
                    candidate.kind != "remnawave",
                    candidate.operator_action_id.in_(
                        select(OperatorAction.id).where(OperatorAction.result != "started")
                    ),
                ),
                or_(candidate.ticket_id.is_(None), ~has_earlier_unfinished),
            )
            .order_by(candidate.created_at, candidate.id)
            .limit(limit)
        )
        async with self.database.session() as session:
            result = await session.execute(
                update(ReconciliationOutbox)
                .where(
                    ReconciliationOutbox.id.in_(claimable_ids),
                    ReconciliationOutbox.status == WorkStatus.PENDING,
                    ReconciliationOutbox.next_attempt_at <= now,
                )
                .values(
                    status=WorkStatus.PROCESSING,
                    claimed_at=now,
                    claim_token=token,
                    attempt_count=ReconciliationOutbox.attempt_count + 1,
                )
                .returning(
                    ReconciliationOutbox.id,
                    ReconciliationOutbox.kind,
                    ReconciliationOutbox.ticket_id,
                    ReconciliationOutbox.operator_action_id,
                    ReconciliationOutbox.payload,
                    ReconciliationOutbox.attempt_count,
                )
            )
            rows = result.all()
            await session.commit()
            return [
                ReconciliationJob(
                    id=row.id,
                    kind=row.kind,
                    ticket_id=row.ticket_id,
                    operator_action_id=row.operator_action_id,
                    payload=row.payload,
                    attempt_count=row.attempt_count,
                    claim_token=token,
                )
                for row in rows
            ]

    async def claim_reconciliation(self) -> ReconciliationJob | None:
        jobs = await self.claim_reconciliations(limit=1)
        return jobs[0] if jobs else None

    async def finish_reconciliation(self, job: ReconciliationJob) -> bool:
        return await self._transition_reconciliation(
            job,
            status=WorkStatus.DELIVERED,
            delivered_at=utcnow(),
            error=None,
        )

    async def retry_reconciliation(
        self,
        job: ReconciliationJob,
        error: str,
        *,
        retry_after_seconds: float | None = None,
        max_attempts: int = MAX_RECONCILIATION_ATTEMPTS,
    ) -> bool:
        terminal = job.attempt_count >= max_attempts
        return await self._transition_reconciliation(
            job,
            status=WorkStatus.FAILED if terminal else WorkStatus.PENDING,
            next_attempt_at=utcnow()
            + timedelta(
                seconds=(
                    retry_after_seconds
                    if retry_after_seconds is not None
                    else min(3600.0, 2.0 ** min(job.attempt_count, 10))
                )
            ),
            error=error,
        )

    async def _transition_reconciliation(
        self,
        job: ReconciliationJob,
        *,
        status: WorkStatus,
        error: str | None,
        next_attempt_at: object | None = None,
        delivered_at: object | None = None,
    ) -> bool:
        values: dict[str, object | None] = {
            "status": status,
            "claimed_at": None,
            "claim_token": None,
            "last_error": error[:1000] if error is not None else None,
        }
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        if delivered_at is not None:
            values["delivered_at"] = delivered_at
        if status in {WorkStatus.DELIVERED, WorkStatus.FAILED}:
            values["payload"] = {}
        async with self.database.session() as session:
            result = await session.execute(
                update(ReconciliationOutbox)
                .where(
                    ReconciliationOutbox.id == job.id,
                    ReconciliationOutbox.status == WorkStatus.PROCESSING,
                    ReconciliationOutbox.claim_token == job.claim_token,
                )
                .values(**values)
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount == 1

    async def release_stale_reconciliations(self, stale_after_seconds: int = 300) -> int:
        threshold = utcnow() - timedelta(seconds=stale_after_seconds)
        async with self.database.session() as session:
            result = await session.execute(
                update(ReconciliationOutbox)
                .where(
                    ReconciliationOutbox.status == WorkStatus.PROCESSING,
                    ReconciliationOutbox.claimed_at < threshold,
                )
                .values(status=WorkStatus.PENDING, claimed_at=None, claim_token=None)
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount
