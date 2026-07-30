from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from suppsystem.database import Database
from suppsystem.durable_work import DurableWorkRepository
from suppsystem.migrations import upgrade_database
from suppsystem.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    InboundUpdate,
    NotificationOutbox,
    NotificationStatus,
    ReconciliationOutbox,
    WorkStatus,
    utcnow,
)
from suppsystem.services import TicketService


@pytest.fixture
async def ticket_service(tmp_path: Path) -> TicketService:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    await database.create_schema_for_tests()
    yield TicketService(database)
    await database.dispose()


async def assert_retention_policy(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=44001,
        display_name="Retention",
        username=None,
    )
    now = utcnow()
    old_inbound = now - timedelta(days=8)
    old_outbox = now - timedelta(days=31)

    async with ticket_service.database.session() as session:
        session.add_all(
            [
                InboundUpdate(
                    telegram_update_id=1,
                    payload={},
                    status=WorkStatus.DELIVERED,
                    processed_at=old_inbound,
                    created_at=old_inbound,
                ),
                InboundUpdate(
                    telegram_update_id=2,
                    payload={},
                    status=WorkStatus.DELIVERED,
                    processed_at=now,
                ),
                InboundUpdate(
                    telegram_update_id=3,
                    payload={"update_id": 3},
                    status=WorkStatus.FAILED,
                    created_at=old_outbox,
                ),
                DeliveryOutbox(
                    id="delivery-old",
                    ticket_id=ticket.id,
                    idempotency_key="delivery-old",
                    direction=Direction.OPERATOR_TO_USER,
                    payload={},
                    status=DeliveryStatus.DELIVERED,
                    delivered_at=old_outbox,
                    created_at=old_outbox,
                ),
                DeliveryOutbox(
                    id="delivery-cancelled",
                    ticket_id=ticket.id,
                    idempotency_key="delivery-cancelled",
                    direction=Direction.OPERATOR_TO_USER,
                    payload={},
                    status=DeliveryStatus.CANCELLED,
                    created_at=old_outbox,
                ),
                DeliveryOutbox(
                    id="delivery-failed",
                    ticket_id=ticket.id,
                    idempotency_key="delivery-failed",
                    direction=Direction.OPERATOR_TO_USER,
                    payload={"text": "retain for diagnostics"},
                    status=DeliveryStatus.FAILED,
                    created_at=old_outbox,
                ),
                NotificationOutbox(
                    id="notification-old",
                    ticket_id=ticket.id,
                    idempotency_key="notification-old",
                    event_type="test",
                    destination="test",
                    recipient_identity_provider="telegram",
                    recipient_identity_value=str(ticket.telegram_user_id),
                    payload={},
                    status=NotificationStatus.DELIVERED,
                    delivered_at=old_outbox,
                    created_at=old_outbox,
                ),
                NotificationOutbox(
                    id="notification-failed",
                    ticket_id=ticket.id,
                    idempotency_key="notification-failed",
                    event_type="test",
                    destination="test",
                    recipient_identity_provider="telegram",
                    recipient_identity_value=str(ticket.telegram_user_id),
                    payload={"reason": "retain for diagnostics"},
                    status=NotificationStatus.FAILED,
                    created_at=old_outbox,
                ),
                ReconciliationOutbox(
                    id="reconciliation-old",
                    idempotency_key="reconciliation-old",
                    kind="telegram_topic",
                    ticket_id=ticket.id,
                    payload={},
                    status=WorkStatus.DELIVERED,
                    delivered_at=old_outbox,
                    created_at=old_outbox,
                ),
                ReconciliationOutbox(
                    id="reconciliation-failed",
                    idempotency_key="reconciliation-failed",
                    kind="telegram_topic",
                    ticket_id=ticket.id,
                    payload={"reason": "retain for diagnostics"},
                    status=WorkStatus.FAILED,
                    created_at=old_outbox,
                ),
            ]
        )
        await session.commit()

    result = await DurableWorkRepository(ticket_service.database).purge_expired_terminal_work(
        now=now
    )

    assert result.inbound_updates == 1
    assert result.delivery_outbox == 2
    assert result.notification_outbox == 1
    assert result.reconciliation_outbox == 1
    assert result.total == 5

    async with ticket_service.database.session() as session:
        inbound_ids = set(await session.scalars(select(InboundUpdate.telegram_update_id)))
        delivery_ids = set(await session.scalars(select(DeliveryOutbox.id)))
        notification_ids = set(await session.scalars(select(NotificationOutbox.id)))
        reconciliation_ids = set(await session.scalars(select(ReconciliationOutbox.id)))

    assert inbound_ids == {2, 3}
    assert delivery_ids == {"delivery-failed"}
    assert notification_ids == {"notification-failed"}
    assert reconciliation_ids == {"reconciliation-failed"}


async def test_retention_prunes_only_expired_successful_or_cancelled_sqlite_work(
    ticket_service: TicketService,
) -> None:
    await assert_retention_policy(ticket_service)


@pytest.mark.postgres
async def test_retention_prunes_only_expired_successful_or_cancelled_postgres_work(
    postgres_database_url: str,
) -> None:
    await upgrade_database(postgres_database_url)
    database = Database(postgres_database_url)
    try:
        await assert_retention_policy(TicketService(database))
    finally:
        await database.dispose()
