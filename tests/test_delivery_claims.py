from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from suppsystem.database import Database
from suppsystem.models import DeliveryOutbox, DeliveryStatus, Direction, utcnow
from suppsystem.services import DeliveryJob, TicketService


@pytest.fixture
async def ticket_service(tmp_path: Path) -> AsyncIterator[TicketService]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    await database.create_schema_for_tests()
    try:
        yield TicketService(database)
    finally:
        await database.dispose()


async def enqueue_and_claim(
    ticket_service: TicketService, *, telegram_user_id: int, key: str
) -> DeliveryJob:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=telegram_user_id,
        display_name="Claim test",
        username=None,
    )
    assert await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="claim me",
        target_chat_id=telegram_user_id,
        idempotency_key=key,
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()
    assert len(jobs) == 1
    return jobs[0]


async def load_delivery(ticket_service: TicketService, delivery_id: str) -> DeliveryOutbox:
    async with ticket_service.database.session() as session:
        delivery = await session.get(DeliveryOutbox, delivery_id)
        assert delivery is not None
        session.expunge(delivery)
        return delivery


async def make_claim_stale(ticket_service: TicketService, delivery_id: str) -> None:
    async with ticket_service.database.session() as session:
        delivery = await session.get(DeliveryOutbox, delivery_id)
        assert delivery is not None
        delivery.claimed_at = utcnow() - timedelta(seconds=301)
        await session.commit()


async def test_delivery_transitions_require_current_processing_claim(
    ticket_service: TicketService,
) -> None:
    job = await enqueue_and_claim(
        ticket_service,
        telegram_user_id=2101,
        key="claim-owner-transitions",
    )

    assert not await ticket_service.outbox.mark_delivery_delivered(
        job.id,
        claim_token="not-the-owner",
        delivered_message_id=999,
    )
    assert not await ticket_service.outbox.mark_delivery_cancelled(
        job.id,
        claim_token="not-the-owner",
        reason="wrong owner",
    )
    assert not await ticket_service.outbox.mark_delivery_retry(
        job.id,
        claim_token="not-the-owner",
        error="wrong owner",
        retry_after_seconds=0,
        max_attempts=8,
    )

    processing = await load_delivery(ticket_service, job.id)
    assert processing.status == DeliveryStatus.PROCESSING
    assert processing.claim_token == job.claim_token
    assert processing.delivered_message_id is None

    assert await ticket_service.outbox.mark_delivery_retry(
        job.id,
        claim_token=job.claim_token,
        error="try again",
        retry_after_seconds=0,
        max_attempts=8,
    )
    pending = await load_delivery(ticket_service, job.id)
    assert pending.status == DeliveryStatus.PENDING
    assert pending.claimed_at is None
    assert pending.claim_token is None

    reclaimed = (await ticket_service.outbox.claim_due_deliveries())[0]
    assert reclaimed.claim_token != job.claim_token
    assert await ticket_service.outbox.mark_delivery_cancelled(
        reclaimed.id,
        claim_token=reclaimed.claim_token,
        reason="cancelled by owner",
    )
    cancelled = await load_delivery(ticket_service, job.id)
    assert cancelled.status == DeliveryStatus.CANCELLED
    assert cancelled.claimed_at is None
    assert cancelled.claim_token is None


async def test_late_worker_cannot_overwrite_released_and_reclaimed_delivery(
    ticket_service: TicketService,
) -> None:
    stale_job = await enqueue_and_claim(
        ticket_service,
        telegram_user_id=2102,
        key="claim-owner-late-worker",
    )
    await make_claim_stale(ticket_service, stale_job.id)

    assert await ticket_service.outbox.release_stale_deliveries() == 1
    released = await load_delivery(ticket_service, stale_job.id)
    assert released.status == DeliveryStatus.PENDING
    assert released.claimed_at is None
    assert released.claim_token is None

    current_job = (await ticket_service.outbox.claim_due_deliveries())[0]
    assert current_job.claim_token != stale_job.claim_token
    assert current_job.attempt_count == 2

    assert not await ticket_service.outbox.mark_delivery_delivered(
        stale_job.id,
        claim_token=stale_job.claim_token,
        delivered_message_id=111,
    )
    still_processing = await load_delivery(ticket_service, stale_job.id)
    assert still_processing.status == DeliveryStatus.PROCESSING
    assert still_processing.claim_token == current_job.claim_token
    assert still_processing.delivered_message_id is None

    assert await ticket_service.outbox.mark_delivery_delivered(
        current_job.id,
        claim_token=current_job.claim_token,
        delivered_message_id=222,
    )
    delivered = await load_delivery(ticket_service, stale_job.id)
    assert delivered.status == DeliveryStatus.DELIVERED
    assert delivered.claimed_at is None
    assert delivered.claim_token is None
    assert delivered.delivered_message_id == 222


async def test_restart_recovers_persisted_stale_delivery_claim(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/restart.db"
    first_database = Database(database_url)
    await first_database.create_schema_for_tests()
    first_service = TicketService(first_database)
    stale_job = await enqueue_and_claim(
        first_service,
        telegram_user_id=2103,
        key="claim-owner-restart",
    )
    await make_claim_stale(first_service, stale_job.id)
    await first_database.dispose()

    restarted_database = Database(database_url)
    try:
        restarted_service = TicketService(restarted_database)
        assert await restarted_service.outbox.release_stale_deliveries() == 1
        recovered_job = (await restarted_service.outbox.claim_due_deliveries())[0]

        assert recovered_job.id == stale_job.id
        assert recovered_job.claim_token != stale_job.claim_token
        assert recovered_job.attempt_count == 2
    finally:
        await restarted_database.dispose()


async def test_shutdown_release_preserves_fifo_and_attempt_budget_across_restart(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/shutdown-release.db"
    first_database = Database(database_url)
    await first_database.create_schema_for_tests()
    first_service = TicketService(first_database)
    ticket = await first_service.open_or_reopen(
        telegram_user_id=2104,
        display_name="Shutdown claim test",
        username=None,
    )
    for index in (1, 2):
        assert await first_service.enqueue_text(
            ticket_id=ticket.id,
            direction=Direction.OPERATOR_TO_USER,
            text=f"ordered {index}",
            target_chat_id=2104,
            idempotency_key=f"shutdown-release-{index}",
        )

    first_job = (await first_service.outbox.claim_due_deliveries())[0]
    assert first_job.attempt_count == 1
    assert (
        await first_service.outbox.release_delivery_claims([(first_job.id, "not-the-owner")]) == 0
    )
    assert (
        await first_service.outbox.release_delivery_claims([(first_job.id, first_job.claim_token)])
        == 1
    )

    released = await load_delivery(first_service, first_job.id)
    assert released.status == DeliveryStatus.PENDING
    assert released.attempt_count == 0
    assert released.claim_token is None
    await first_database.dispose()

    restarted_database = Database(database_url)
    try:
        restarted_service = TicketService(restarted_database)
        reclaimed = (await restarted_service.outbox.claim_due_deliveries())[0]

        assert reclaimed.id == first_job.id
        assert reclaimed.claim_token != first_job.claim_token
        assert reclaimed.attempt_count == 1
        assert not await restarted_service.outbox.release_delivery_claims(
            [(reclaimed.id, first_job.claim_token)]
        )
        processing = await load_delivery(restarted_service, reclaimed.id)
        assert processing.status == DeliveryStatus.PROCESSING
        assert processing.claim_token == reclaimed.claim_token
    finally:
        await restarted_database.dispose()
