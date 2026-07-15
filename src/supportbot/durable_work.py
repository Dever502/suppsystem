from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy import case, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportbot.database import Database
from supportbot.models import (
    InboundUpdate,
    ReconciliationOutbox,
    WorkStatus,
    utcnow,
)


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


async def enqueue_topic_reconciliation(
    session: AsyncSession,
    *,
    ticket_id: str,
    desired_status: str,
) -> None:
    key = f"topic:{ticket_id}"
    entry = await session.scalar(
        select(ReconciliationOutbox).where(ReconciliationOutbox.idempotency_key == key)
    )
    if entry is None:
        session.add(
            ReconciliationOutbox(
                idempotency_key=key,
                kind="telegram_topic",
                ticket_id=ticket_id,
                payload={"desired_status": desired_status},
            )
        )
        return
    entry.payload = {"desired_status": desired_status}
    entry.status = WorkStatus.PENDING
    entry.attempt_count = 0
    entry.next_attempt_at = utcnow()
    entry.claimed_at = None
    entry.claim_token = None
    entry.delivered_at = None
    entry.last_error = None


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
        self, telegram_update_id: int, payload: dict[str, object]
    ) -> bool:
        async with self.database.session() as session:
            if await session.get(InboundUpdate, telegram_update_id) is not None:
                return False
            session.add(InboundUpdate(telegram_update_id=telegram_update_id, payload=payload))
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
        candidate = (
            select(InboundUpdate.telegram_update_id)
            .where(
                InboundUpdate.status == WorkStatus.PENDING,
                InboundUpdate.next_attempt_at <= now,
            )
            .order_by(InboundUpdate.telegram_update_id)
            .limit(1)
        )
        async with self.database.session() as session:
            result = await session.execute(
                update(InboundUpdate)
                .where(
                    InboundUpdate.telegram_update_id.in_(candidate),
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

    async def claim_reconciliation(self) -> ReconciliationJob | None:
        now = utcnow()
        token = str(uuid.uuid4())
        candidate = (
            select(ReconciliationOutbox.id)
            .where(
                ReconciliationOutbox.status == WorkStatus.PENDING,
                ReconciliationOutbox.next_attempt_at <= now,
            )
            .order_by(ReconciliationOutbox.created_at, ReconciliationOutbox.id)
            .limit(1)
        )
        async with self.database.session() as session:
            result = await session.execute(
                update(ReconciliationOutbox)
                .where(
                    ReconciliationOutbox.id.in_(candidate),
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
            row = result.first()
            await session.commit()
            if row is None:
                return None
            return ReconciliationJob(
                id=row.id,
                kind=row.kind,
                ticket_id=row.ticket_id,
                operator_action_id=row.operator_action_id,
                payload=row.payload,
                attempt_count=row.attempt_count,
                claim_token=token,
            )

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
        max_attempts: int = 20,
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
