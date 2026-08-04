from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from suppsystem.database import Database
from suppsystem.durable_work import DurableWorkRepository
from suppsystem.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    InboundUpdate,
    NotificationOutbox,
    OperatorAction,
    TicketMessage,
    TicketStatus,
    WorkStatus,
    utcnow,
)
from suppsystem.outbox_repository import OutboxRepository
from suppsystem.service_types import TicketNotFoundError, TicketView, TopicProvisioningConflictError
from suppsystem.services import TicketService


@pytest.fixture
async def ticket_service(tmp_path: Path) -> TicketService:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    await database.create_schema_for_tests()
    yield TicketService(database)
    await database.dispose()


async def test_ticket_service_retains_legacy_outbox_api(
    ticket_service: TicketService,
) -> None:
    assert isinstance(ticket_service, OutboxRepository)
    assert await ticket_service.claim_due_deliveries() == []
    assert await ticket_service.claim_due_notifications() == []


async def attach_claimed_topic(
    ticket_service: TicketService, ticket_id: str, topic_id: int
) -> TicketView:
    token = await ticket_service.claim_topic_provisioning(ticket_id)
    assert token is not None
    return await ticket_service.attach_topic(ticket_id, topic_id, token=token)


async def enqueue_subscription_notification(
    ticket_service: TicketService, ticket: TicketView, idempotency_key: str
) -> bool:
    return await ticket_service.enqueue_notification(
        ticket_id=ticket.id,
        event_type="subscription_link_reissued",
        destination="subscription_owner",
        recipient_identity_provider="telegram",
        recipient_identity_value=str(ticket.telegram_user_id),
        payload={"subscription_url": "https://sub.example/new"},
        idempotency_key=idempotency_key,
    )


async def test_one_user_reuses_one_ticket_and_topic(ticket_service: TicketService) -> None:
    opened = await ticket_service.open_or_reopen(
        telegram_user_id=1001, display_name="Alice", username="alice"
    )
    assert opened.status is TicketStatus.PROVISIONING
    assert opened.topic_id is None

    attached = await attach_claimed_topic(ticket_service, opened.id, 777)
    assert attached.status is TicketStatus.OPEN
    assert attached.topic_id == 777

    await ticket_service.close(ticket_id=opened.id, operator_telegram_id=42)
    reopened = await ticket_service.open_or_reopen(
        telegram_user_id=1001, display_name="Alice Updated", username="alice"
    )

    assert reopened.id == opened.id
    assert reopened.topic_id == 777
    assert reopened.status is TicketStatus.OPEN
    assert reopened.display_name == "Alice Updated"


async def test_message_updates_ticket_last_activity(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1099, display_name="Activity", username=None
    )
    before = ticket.last_activity_at

    assert await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="new activity",
        target_chat_id=ticket.telegram_user_id,
        idempotency_key="activity-message-1",
    )

    refreshed = await ticket_service.get_ticket(ticket.id)
    assert refreshed.last_activity_at > before.replace(tzinfo=None)


async def test_ticket_listing_eager_loads_users_and_identities(
    ticket_service: TicketService,
) -> None:
    ticket_ids: dict[int, str] = {}
    for telegram_id in range(1100, 1103):
        ticket = await ticket_service.open_or_reopen(
            telegram_user_id=telegram_id,
            display_name=f"User {telegram_id}",
            username=None,
        )
        ticket_ids[telegram_id] = ticket.id
    await ticket_service.enqueue_text(
        ticket_id=ticket_ids[1100],
        direction=Direction.OPERATOR_TO_USER,
        text="most recent",
        target_chat_id=1100,
        idempotency_key="listing-order-activity",
    )

    statements: list[str] = []

    def count_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(ticket_service.database.engine.sync_engine, "before_cursor_execute", count_query)
    try:
        tickets = await ticket_service.list_tickets(status=None, limit=50, offset=0)
    finally:
        event.remove(
            ticket_service.database.engine.sync_engine,
            "before_cursor_execute",
            count_query,
        )

    assert len(tickets) == 3
    assert len(statements) == 2
    assert tickets[0].telegram_user_id == 1100


async def test_internal_note_is_saved_without_delivery(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1013, display_name="Kate", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 785)

    first = await ticket_service.add_internal_note(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        operator_display_name="Alice Operator",
        operator_username="alice",
        note="Call back after 12:00",
        source_chat_id=-100123,
        source_message_id=77,
        idempotency_key="telegram:-100:77:/note",
    )
    duplicate = await ticket_service.add_internal_note(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        operator_display_name="Alice Operator",
        operator_username="alice",
        note="Call back after 12:00",
        source_chat_id=-100123,
        source_message_id=77,
        idempotency_key="telegram:-100:77:/note",
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert first is True
    assert duplicate is False
    assert jobs == []
    notes = await ticket_service.list_internal_notes(ticket.id)
    assert len(notes) == 1
    assert notes[0].content == "Call back after 12:00"
    assert notes[0].operator_telegram_id == 42
    assert notes[0].operator_display_name == "Alice Operator"
    assert notes[0].operator_username == "alice"


async def test_delivery_enqueue_is_idempotent(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1002, display_name="Bob", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 778)

    first = await ticket_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=1002,
        source_message_id=5,
        target_chat_id=-100123,
        target_thread_id=778,
    )
    duplicate = await ticket_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=1002,
        source_message_id=5,
        target_chat_id=-100123,
        target_thread_id=778,
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert first is True
    assert duplicate is False
    assert len(jobs) == 1


async def test_delivery_receipt_is_persisted(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=10023, display_name="Receipt User", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 90023)
    await ticket_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=10023,
        source_message_id=1,
        target_chat_id=-100123,
        target_thread_id=90023,
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    await ticket_service.outbox.mark_delivery_delivered(
        jobs[0].id,
        claim_token=jobs[0].claim_token,
        delivered_message_id=456,
    )

    async with ticket_service.database.session() as session:
        delivery = await session.get(DeliveryOutbox, jobs[0].id)
    assert delivery is not None
    assert delivery.status == DeliveryStatus.DELIVERED
    assert delivery.delivered_message_id == 456
    assert delivery.delivered_at is not None
    assert delivery.payload == {}


async def test_user_message_waits_for_topic_and_is_released_on_attach(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=10020, display_name="Persist First", username=None
    )
    queued = await ticket_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=10020,
        source_message_id=1,
        target_chat_id=-100123,
        target_thread_id=None,
        content="first message",
    )

    assert queued is True
    assert await ticket_service.outbox.claim_due_deliveries() == []

    await attach_claimed_topic(ticket_service, ticket.id, 90020)
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert len(jobs) == 1
    assert jobs[0].payload["target_thread_id"] == 90020


async def test_enqueue_uses_current_topic_when_caller_has_stale_ticket_view(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=10022, display_name="Stale View", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 90022)

    queued = await ticket_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=10022,
        source_message_id=1,
        target_chat_id=-100123,
        target_thread_id=None,
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert queued is True
    assert len(jobs) == 1
    assert jobs[0].payload["target_thread_id"] == 90022


async def test_delayed_retry_blocks_later_delivery_in_same_ticket(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=10021, display_name="FIFO User", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 90021)
    for message_id in (1, 2):
        await ticket_service.enqueue_copy(
            ticket_id=ticket.id,
            direction=Direction.USER_TO_OPERATOR,
            source_chat_id=10021,
            source_message_id=message_id,
            target_chat_id=-100123,
            target_thread_id=90021,
        )

    first_batch = await ticket_service.outbox.claim_due_deliveries()
    assert len(first_batch) == 1
    assert first_batch[0].payload["source_message_id"] == 1

    await ticket_service.outbox.mark_delivery_retry(
        first_batch[0].id,
        claim_token=first_batch[0].claim_token,
        error="temporary failure",
        retry_after_seconds=60,
        max_attempts=8,
    )
    assert await ticket_service.outbox.claim_due_deliveries() == []

    async with ticket_service.database.session() as session:
        retriable = await session.get(DeliveryOutbox, first_batch[0].id)
        assert retriable is not None
        retriable.next_attempt_at = utcnow()
        await session.commit()

    retry_batch = await ticket_service.outbox.claim_due_deliveries()
    assert len(retry_batch) == 1
    assert retry_batch[0].id == first_batch[0].id
    assert retry_batch[0].claim_token != first_batch[0].claim_token
    assert await ticket_service.outbox.mark_delivery_delivered(
        retry_batch[0].id,
        claim_token=retry_batch[0].claim_token,
    )
    second_batch = await ticket_service.outbox.claim_due_deliveries()
    assert len(second_batch) == 1
    assert second_batch[0].payload["source_message_id"] == 2


async def test_blocklist_is_enforced(ticket_service: TicketService) -> None:
    await ticket_service.block(telegram_user_id=1003, operator_telegram_id=42, reason="spam")
    assert await ticket_service.is_blocked(1003) is True
    assert await ticket_service.unblock(telegram_user_id=1003) is True
    assert await ticket_service.is_blocked(1003) is False


async def test_topic_provisioning_claim_is_exclusive(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1004, display_name="Carol", username=None
    )

    first_claim = await ticket_service.claim_topic_provisioning(ticket.id)
    second_claim = await ticket_service.claim_topic_provisioning(ticket.id)

    assert first_claim is not None
    assert second_claim is None

    await ticket_service.abort_topic_provisioning(ticket_id=ticket.id, token=first_claim)
    assert await ticket_service.claim_topic_provisioning(ticket.id) is not None


async def test_attach_topic_rejects_cancelled_provisioning_claim(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=10040, display_name="Stale Claim", username=None
    )
    stale_token = await ticket_service.claim_topic_provisioning(ticket.id)
    assert stale_token is not None
    assert await ticket_service.reset_topic_provisioning(ticket.id) is True
    current_token = await ticket_service.claim_topic_provisioning(ticket.id)
    assert current_token is not None

    with pytest.raises(TopicProvisioningConflictError):
        await ticket_service.attach_topic(ticket.id, 940, token=stale_token)

    attached = await ticket_service.attach_topic(ticket.id, 941, token=current_token)

    assert attached.status is TicketStatus.OPEN
    assert attached.topic_id == 941


async def test_close_cancels_claim_and_rejects_late_topic_attachment(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=10041, display_name="Close Claim", username=None
    )
    token = await ticket_service.claim_topic_provisioning(ticket.id)
    assert token is not None

    assert await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="close-active-topic-claim",
    )
    with pytest.raises(TopicProvisioningConflictError):
        await ticket_service.attach_topic(ticket.id, 942, token=token)

    closed = await ticket_service.get_ticket(ticket.id)
    assert closed.status is TicketStatus.CLOSED
    assert closed.closed_at is not None
    assert closed.topic_id is None
    assert await ticket_service.claim_topic_provisioning(ticket.id) is None


async def test_missing_topic_preserves_closed_ticket_state(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=10042, display_name="Closed Missing Topic", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 943)
    assert await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="close-before-topic-invalidation",
    )
    closed_before = await ticket_service.get_ticket(ticket.id)

    await ticket_service.invalidate_topic(ticket_id=ticket.id, topic_id=943)

    closed_after = await ticket_service.get_ticket(ticket.id)
    assert closed_after.status is TicketStatus.CLOSED
    assert closed_after.closed_at == closed_before.closed_at
    assert closed_after.topic_id is None
    assert await ticket_service.claim_topic_provisioning(ticket.id) is None
    assert await ticket_service.claim_closed_topic_recovery(ticket.id) is None


async def test_operator_close_is_idempotent(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1005, display_name="Dana", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 779)

    first = await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="telegram:-100:10:/stop",
    )
    duplicate = await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="telegram:-100:10:/stop",
    )

    assert first is True
    assert duplicate is False


async def test_concurrent_close_has_one_winner(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1050, display_name="Concurrent Close", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 795)

    results = await asyncio.gather(
        ticket_service.close(
            ticket_id=ticket.id,
            operator_telegram_id=42,
            idempotency_key="concurrent-close-1",
        ),
        ticket_service.close(
            ticket_id=ticket.id,
            operator_telegram_id=43,
            idempotency_key="concurrent-close-2",
        ),
    )

    async with ticket_service.database.session() as session:
        actions = list(
            (
                await session.scalars(
                    select(OperatorAction).where(
                        OperatorAction.ticket_id == ticket.id,
                        OperatorAction.action == "close_ticket",
                    )
                )
            ).all()
        )

    assert sorted(results) == [False, True]
    assert len(actions) == 1


async def test_concurrent_close_and_topic_attach_keep_closed_state_consistent(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1052, display_name="Concurrent Topic Close", username=None
    )
    token = await ticket_service.claim_topic_provisioning(ticket.id)
    assert token is not None

    async def attach_if_current() -> bool:
        try:
            await ticket_service.attach_topic(ticket.id, 945, token=token)
        except TopicProvisioningConflictError:
            return False
        return True

    close_changed, attach_changed = await asyncio.gather(
        ticket_service.close(
            ticket_id=ticket.id,
            operator_telegram_id=42,
            idempotency_key="concurrent-topic-close",
        ),
        attach_if_current(),
    )

    current = await ticket_service.get_ticket(ticket.id)
    assert close_changed is True
    assert current.status is TicketStatus.CLOSED
    assert current.closed_at is not None
    assert current.topic_id == (945 if attach_changed else None)


async def test_close_all_conditionally_closes_open_and_provisioning_tickets(
    ticket_service: TicketService,
) -> None:
    provisioning = await ticket_service.open_or_reopen(
        telegram_user_id=1053, display_name="Provisioning Bulk Close", username=None
    )
    stale_token = await ticket_service.claim_topic_provisioning(provisioning.id)
    assert stale_token is not None
    opened = await ticket_service.open_or_reopen(
        telegram_user_id=1054, display_name="Open Bulk Close", username=None
    )
    await attach_claimed_topic(ticket_service, opened.id, 946)

    closed = await ticket_service.close_all(
        operator_telegram_id=42,
        idempotency_key="conditional-close-all",
    )

    assert {ticket.id for ticket in closed} == {provisioning.id, opened.id}
    for ticket_id in (provisioning.id, opened.id):
        current = await ticket_service.get_ticket(ticket_id)
        assert current.status is TicketStatus.CLOSED
        assert current.closed_at is not None
        assert current.close_cycle == 1
    assert await ticket_service.claim_topic_provisioning(provisioning.id) is None
    with pytest.raises(TopicProvisioningConflictError):
        await ticket_service.attach_topic(provisioning.id, 947, token=stale_token)


async def test_concurrent_outbox_enqueue_is_idempotent(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1051, display_name="Concurrent Outbox", username=None
    )

    results = await asyncio.gather(
        *(
            ticket_service.enqueue_text(
                ticket_id=ticket.id,
                direction=Direction.OPERATOR_TO_USER,
                text="One durable message",
                target_chat_id=1051,
                idempotency_key="concurrent-outbox",
            )
            for _ in range(2)
        )
    )

    async with ticket_service.database.session() as session:
        deliveries = list(
            (
                await session.scalars(
                    select(DeliveryOutbox).where(
                        DeliveryOutbox.idempotency_key == "concurrent-outbox"
                    )
                )
            ).all()
        )
        messages = list(
            (
                await session.scalars(
                    select(TicketMessage).where(TicketMessage.ticket_id == ticket.id)
                )
            ).all()
        )

    assert sorted(results) == [False, True]
    assert len(deliveries) == 1
    assert len(messages) == 1


async def test_operator_close_can_enqueue_user_notification(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1010, display_name="Helen", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 782)

    changed = await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="telegram:-100:11:/stop",
        notification_text="Тикет закрыт.",
        notification_target_chat_id=1010,
        notification_idempotency_key="telegram:-100:11:/stop:user-notification",
        notification_parse_mode="HTML",
    )
    duplicate = await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="telegram:-100:11:/stop",
        notification_text="Тикет закрыт.",
        notification_target_chat_id=1010,
        notification_idempotency_key="telegram:-100:11:/stop:user-notification",
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert changed is True
    assert duplicate is False
    assert len(jobs) == 1
    assert jobs[0].payload == {
        "kind": "send_text",
        "target_chat_id": 1010,
        "text": "Тикет закрыт.",
        "parse_mode": "HTML",
    }


async def test_retarget_topic_deliveries_requeues_all_unfinished_user_messages(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=2020, display_name="Recovery User", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 900)
    for message_id in (1, 2):
        await ticket_service.enqueue_copy(
            ticket_id=ticket.id,
            direction=Direction.USER_TO_OPERATOR,
            source_chat_id=2020,
            source_message_id=message_id,
            target_chat_id=-100123,
            target_thread_id=900,
        )

    claimed = await ticket_service.outbox.claim_due_deliveries()
    assert len(claimed) == 1

    retargeted = await ticket_service.retarget_topic_deliveries(
        ticket_id=ticket.id, old_topic_id=900, new_topic_id=901
    )
    first_requeued = await ticket_service.outbox.claim_due_deliveries()

    assert retargeted == 2
    assert len(first_requeued) == 1
    assert first_requeued[0].payload["target_thread_id"] == 901
    assert first_requeued[0].attempt_count == 1

    await ticket_service.outbox.mark_delivery_delivered(
        first_requeued[0].id,
        claim_token=first_requeued[0].claim_token,
    )
    second_requeued = await ticket_service.outbox.claim_due_deliveries()
    assert len(second_requeued) == 1
    assert second_requeued[0].payload["target_thread_id"] == 901
    assert second_requeued[0].attempt_count == 1


async def test_operator_close_notification_can_include_rating_keyboard(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1011, display_name="Iris", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 783)

    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="telegram:-100:12:/stop",
        notification_text="Оцените поддержку.",
        notification_target_chat_id=1011,
        notification_idempotency_key="telegram:-100:12:/stop:user-notification",
        notification_reply_markup={
            "inline_keyboard": [[{"text": "⭐ 1", "callback_data": "suppsystem_rating:ticket:1"}]]
        },
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert jobs[0].payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "⭐ 1", "callback_data": "suppsystem_rating:ticket:1"}]]
    }


async def test_close_notification_builder_uses_committed_close_cycle(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1016, display_name="Rating Builder", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 789)
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="rating-builder-close-1",
    )
    await ticket_service.reopen(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="rating-builder-reopen",
    )
    built_cycles: list[int] = []

    def build_keyboard(close_cycle: int) -> dict[str, object]:
        built_cycles.append(close_cycle)
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "⭐ 5",
                        "callback_data": f"suppsystem_rating:{ticket.id}:{close_cycle}:5",
                    }
                ]
            ]
        }

    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="rating-builder-close-2",
        notification_text="Оцените поддержку.",
        notification_target_chat_id=ticket.telegram_user_id,
        notification_idempotency_key="rating-builder-notification-2",
        notification_reply_markup_builder=build_keyboard,
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert built_cycles == [2]
    assert jobs[0].payload["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "⭐ 5", "callback_data": f"suppsystem_rating:{ticket.id}:2:5"}]
        ]
    }


async def test_rating_enqueue_targets_ratings_system_topic(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1012, display_name="Jack", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 784)
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="close-for-rating",
    )
    ticket = await ticket_service.get_ticket(ticket.id)

    first = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=1012,
        score=5,
        close_cycle=ticket.close_cycle,
        target_chat_id=-100123,
        text="Оценка: ⭐ 5/5",
        idempotency_key=f"rating:{ticket.id}:1012",
        parse_mode="HTML",
    )
    duplicate = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=1012,
        score=5,
        close_cycle=ticket.close_cycle,
        target_chat_id=-100123,
        text="Оценка: ⭐ 5/5",
        idempotency_key=f"rating:{ticket.id}:1012",
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert first is True
    assert duplicate is False
    assert len(jobs) == 1
    assert jobs[0].payload == {
        "kind": "send_text",
        "target_chat_id": -100123,
        "target_system_topic": "ratings",
        "text": "Оценка: ⭐ 5/5",
        "parse_mode": "HTML",
    }


async def test_each_close_cycle_accepts_one_current_rating(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1014, display_name="Rating Cycles", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 786)
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="rating-cycle-close-1",
    )
    first_closed = await ticket_service.get_ticket(ticket.id)

    first = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=1014,
        score=4,
        close_cycle=first_closed.close_cycle,
        target_chat_id=-100123,
        text="First cycle: 4/5",
        idempotency_key="rating-cycle-1",
    )
    duplicate_cycle = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=1014,
        score=3,
        close_cycle=first_closed.close_cycle,
        target_chat_id=-100123,
        text="Duplicate cycle",
        idempotency_key="rating-cycle-1-different-key",
    )
    await ticket_service.reopen(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="rating-cycle-reopen",
    )
    stale = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=1014,
        score=5,
        close_cycle=first_closed.close_cycle,
        target_chat_id=-100123,
        text="Stale cycle",
        idempotency_key="rating-cycle-stale",
    )
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="rating-cycle-close-2",
    )
    second_closed = await ticket_service.get_ticket(ticket.id)
    second = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=1014,
        score=5,
        close_cycle=second_closed.close_cycle,
        target_chat_id=-100123,
        text="Second cycle: 5/5",
        idempotency_key="rating-cycle-2",
    )

    async with ticket_service.database.session() as session:
        ratings = list(
            (
                await session.scalars(
                    select(TicketMessage)
                    .where(
                        TicketMessage.ticket_id == ticket.id,
                        TicketMessage.channel == "rating",
                    )
                    .order_by(TicketMessage.rating_cycle)
                )
            ).all()
        )

    assert first is True
    assert duplicate_cycle is False
    assert stale is False
    assert second is True
    assert second_closed.close_cycle == first_closed.close_cycle + 1
    assert [rating.rating_cycle for rating in ratings] == [1, 2]


async def test_each_unrated_close_cycle_remains_independently_rateable(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1017, display_name="Independent Ratings", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 790)
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="independent-rating-close-1",
    )
    first_closed = await ticket_service.get_ticket(ticket.id)
    await ticket_service.reopen(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="independent-rating-reopen",
    )
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="independent-rating-close-2",
    )
    second_closed = await ticket_service.get_ticket(ticket.id)

    first = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=ticket.telegram_user_id,
        score=4,
        close_cycle=first_closed.close_cycle,
        target_chat_id=-100123,
        text="First cycle: 4/5",
        idempotency_key="independent-rating-cycle-1",
    )
    second = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=ticket.telegram_user_id,
        score=5,
        close_cycle=second_closed.close_cycle,
        target_chat_id=-100123,
        text="Second cycle: 5/5",
        idempotency_key="independent-rating-cycle-2",
    )
    duplicate_first = await ticket_service.enqueue_rating(
        ticket_id=ticket.id,
        source_chat_id=ticket.telegram_user_id,
        score=3,
        close_cycle=first_closed.close_cycle,
        target_chat_id=-100123,
        text="Duplicate first cycle",
        idempotency_key="independent-rating-cycle-1-duplicate",
    )

    assert first is True
    assert second is True
    assert duplicate_first is False


async def test_rating_remains_valid_during_reopen(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1018, display_name="Concurrent Rating", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 948)
    assert await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="rating-race-close",
    )
    closed = await ticket_service.get_ticket(ticket.id)

    rating_accepted, reopened = await asyncio.gather(
        ticket_service.enqueue_rating(
            ticket_id=ticket.id,
            source_chat_id=1018,
            score=5,
            close_cycle=closed.close_cycle,
            target_chat_id=-100123,
            text="Concurrent cycle: 5/5",
            idempotency_key="rating-race-cycle",
        ),
        ticket_service.reopen(
            ticket_id=ticket.id,
            operator_telegram_id=42,
            idempotency_key="rating-race-reopen",
        ),
    )

    current = await ticket_service.get_ticket(ticket.id)
    async with ticket_service.database.session() as session:
        ratings = list(
            (
                await session.scalars(
                    select(TicketMessage).where(
                        TicketMessage.ticket_id == ticket.id,
                        TicketMessage.channel == "rating",
                    )
                )
            ).all()
        )

    assert reopened is True
    assert rating_accepted is True
    assert current.status is TicketStatus.OPEN
    assert current.closed_at is None
    assert len(ratings) == 1


async def test_api_text_reply_uses_durable_outbox(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1006, display_name="Erin", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 780)

    queued = await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="A plain API reply",
        target_chat_id=1006,
        idempotency_key="api:message:1006:one",
    )
    duplicate = await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="A plain API reply",
        target_chat_id=1006,
        idempotency_key="api:message:1006:one",
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert queued is True
    assert duplicate is False
    assert jobs[0].payload["kind"] == "send_text"


async def test_reopening_ticket_reports_transition(ticket_service: TicketService) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1007, display_name="Frank", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 781)
    await ticket_service.close(ticket_id=ticket.id, operator_telegram_id=42)

    reopened = await ticket_service.open_or_reopen(
        telegram_user_id=1007, display_name="Frank", username=None
    )

    assert reopened.status is TicketStatus.OPEN
    assert reopened.reopened is True


async def test_reopened_customer_delivery_requires_context_first(
    ticket_service: TicketService,
) -> None:
    first = await ticket_service.accept_customer_message(
        telegram_user_id=10071,
        display_name="Frank",
        username=None,
        source_chat_id=10071,
        source_message_id=1,
        target_chat_id=-100123,
        content="First message",
        media=None,
    )
    assert first.ticket is not None
    await attach_claimed_topic(ticket_service, first.ticket.id, 1781)
    first_delivery = (await ticket_service.outbox.claim_due_deliveries())[0]
    assert await ticket_service.outbox.mark_delivery_delivered(
        first_delivery.id,
        claim_token=first_delivery.claim_token,
    )
    await ticket_service.close(ticket_id=first.ticket.id, operator_telegram_id=42)

    reopened = await ticket_service.accept_customer_message(
        telegram_user_id=10071,
        display_name="Frank",
        username=None,
        source_chat_id=10071,
        source_message_id=2,
        target_chat_id=-100123,
        content="Second message",
        media=None,
    )

    delivery = (await ticket_service.outbox.claim_due_deliveries())[0]
    assert reopened.reopened is True
    assert delivery.payload["prepare_reopened_context"] is True
    assert await ticket_service.outbox.mark_reopened_context_prepared(
        delivery.id,
        claim_token=delivery.claim_token,
    )
    assert await ticket_service.outbox.mark_delivery_retry(
        delivery.id,
        claim_token=delivery.claim_token,
        error="temporary Telegram failure",
        retry_after_seconds=0,
        max_attempts=8,
    )

    retried = (await ticket_service.outbox.claim_due_deliveries())[0]

    assert "prepare_reopened_context" not in retried.payload


async def test_topic_provisioning_retry_requires_explicit_reset(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1008, display_name="Grace", username=None
    )

    first_claim = await ticket_service.claim_topic_provisioning(ticket.id)
    assert first_claim is not None
    assert await ticket_service.claim_topic_provisioning(ticket.id) is None

    assert await ticket_service.reset_topic_provisioning(ticket.id) is True
    assert await ticket_service.claim_topic_provisioning(ticket.id) is not None


async def test_blocklist_blocks_operator_to_user_deliveries(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1014, display_name="Liam", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 786)
    await ticket_service.block(telegram_user_id=1014, operator_telegram_id=42)

    copied = await ticket_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        source_chat_id=-100123,
        source_message_id=91,
        target_chat_id=1014,
    )
    text = await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="blocked reply",
        target_chat_id=1014,
        idempotency_key="api:blocked:reply",
    )
    closed = await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="telegram:-100:91:/stop",
        notification_text="closed",
        notification_target_chat_id=1014,
        notification_idempotency_key="telegram:-100:91:/stop:user-notification",
    )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert copied is False
    assert text is False
    assert closed is True
    assert jobs == []


async def test_notification_outbox_enqueue_is_idempotent(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1015, display_name="Mia", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 787)

    first = await enqueue_subscription_notification(ticket_service, ticket, "notification:one")
    duplicate = await enqueue_subscription_notification(ticket_service, ticket, "notification:one")

    async with ticket_service.database.session() as session:
        notifications = list((await session.scalars(select(NotificationOutbox))).all())

    assert first is True
    assert duplicate is False
    assert len(notifications) == 1
    assert notifications[0].destination == "subscription_owner"
    assert notifications[0].payload == {"subscription_url": "https://sub.example/new"}


async def test_notification_outbox_claim_and_mark_delivered(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1016, display_name="Nia", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 788)
    await enqueue_subscription_notification(ticket_service, ticket, "notification:claim-one")

    jobs = await ticket_service.outbox.claim_due_notifications()
    await ticket_service.outbox.mark_notification_delivered(
        jobs[0].id, claim_token=jobs[0].claim_token
    )

    async with ticket_service.database.session() as session:
        notification = await session.get(NotificationOutbox, jobs[0].id)

    assert len(jobs) == 1
    assert jobs[0].event_type == "subscription_link_reissued"
    assert jobs[0].attempt_count == 1
    assert notification is not None
    assert notification.status == "delivered"
    assert notification.delivered_at is not None
    assert notification.payload == {}


async def test_notification_outbox_retry_marks_failed_after_max_attempts(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=1017, display_name="Ola", username=None
    )
    await attach_claimed_topic(ticket_service, ticket.id, 789)
    await enqueue_subscription_notification(ticket_service, ticket, "notification:fail-one")

    jobs = await ticket_service.outbox.claim_due_notifications()
    await ticket_service.outbox.mark_notification_retry(
        jobs[0].id,
        claim_token=jobs[0].claim_token,
        error="bad request",
        retry_after_seconds=0,
        max_attempts=jobs[0].attempt_count,
    )

    async with ticket_service.database.session() as session:
        notification = await session.get(NotificationOutbox, jobs[0].id)

    assert notification is not None
    assert notification.status == "failed"
    assert notification.last_error == "bad request"
    assert notification.payload == {}


async def test_inbound_update_claim_fencing_survives_reclaim(
    ticket_service: TicketService,
) -> None:
    repository = DurableWorkRepository(ticket_service.database)
    assert (
        await repository.enqueue_inbound_update(
            7001, {"update_id": 7001}, ordering_key="chat:7:thread:0"
        )
        is True
    )
    assert (
        await repository.enqueue_inbound_update(
            7001, {"update_id": 7001}, ordering_key="chat:7:thread:0"
        )
        is False
    )

    old_claim = await repository.claim_inbound_update()
    assert old_claim is not None
    assert await repository.release_stale_inbound_updates(stale_after_seconds=0) == 1
    new_claim = await repository.claim_inbound_update()
    assert new_claim is not None
    assert old_claim.claim_token != new_claim.claim_token
    assert await repository.finish_inbound_update(old_claim) is False
    assert await repository.finish_inbound_update(new_claim) is True

    async with ticket_service.database.session() as session:
        update = await session.get(InboundUpdate, 7001)
    assert update is not None
    assert WorkStatus(update.status) is WorkStatus.DELIVERED
    assert update.payload == {}


async def test_inbound_retry_blocks_only_later_updates_with_same_ordering_key(
    ticket_service: TicketService,
) -> None:
    repository = DurableWorkRepository(ticket_service.database)
    await repository.enqueue_inbound_update(
        7101, {"update_id": 7101}, ordering_key="chat:7:thread:11"
    )
    await repository.enqueue_inbound_update(
        7102, {"update_id": 7102}, ordering_key="chat:7:thread:11"
    )
    await repository.enqueue_inbound_update(
        7103, {"update_id": 7103}, ordering_key="chat:7:thread:12"
    )

    first = await repository.claim_inbound_update()
    assert first is not None and first.telegram_update_id == 7101
    assert await repository.retry_inbound_update(first, "temporary failure")

    independent = await repository.claim_inbound_update()
    assert independent is not None and independent.telegram_update_id == 7103
    assert await repository.finish_inbound_update(independent)
    assert await repository.claim_inbound_update() is None

    async with ticket_service.database.session() as session:
        retried = await session.get(InboundUpdate, 7101)
        assert retried is not None
        retried.next_attempt_at = utcnow()
        await session.commit()

    retried = await repository.claim_inbound_update()
    assert retried is not None and retried.telegram_update_id == 7101
    assert await repository.finish_inbound_update(retried)
    later = await repository.claim_inbound_update()
    assert later is not None and later.telegram_update_id == 7102


async def test_notification_claim_fencing_rejects_late_worker(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=17001, display_name="Webhook fencing", username=None
    )
    assert await ticket_service.enqueue_notification(
        ticket_id=ticket.id,
        event_type="test",
        destination="test",
        recipient_identity_provider="telegram",
        recipient_identity_value=str(ticket.telegram_user_id),
        payload={},
        idempotency_key="notification:fencing",
    )
    old_claim = (await ticket_service.outbox.claim_due_notifications(limit=1))[0]
    assert await ticket_service.outbox.release_stale_notifications(stale_after_seconds=0) == 1
    new_claim = (await ticket_service.outbox.claim_due_notifications(limit=1))[0]

    assert (
        await ticket_service.outbox.mark_notification_delivered(
            old_claim.id, claim_token=old_claim.claim_token
        )
        is False
    )
    assert (
        await ticket_service.outbox.mark_notification_delivered(
            new_claim.id, claim_token=new_claim.claim_token
        )
        is True
    )


async def test_atomic_telegram_ingress_commands_are_idempotent(
    ticket_service: TicketService,
) -> None:
    first = await ticket_service.accept_customer_message(
        telegram_user_id=18001,
        display_name="Atomic user",
        username="atomic",
        source_chat_id=18001,
        source_message_id=10,
        target_chat_id=-100123,
        content="help",
        media=None,
    )
    duplicate = await ticket_service.accept_customer_message(
        telegram_user_id=18001,
        display_name="Atomic user",
        username="atomic",
        source_chat_id=18001,
        source_message_id=10,
        target_chat_id=-100123,
        content="help",
        media=None,
    )
    assert first.changed is True and first.ticket is not None
    assert duplicate.changed is False

    ticket = first.ticket
    await ticket_service.close(
        ticket_id=ticket.id, operator_telegram_id=42, idempotency_key="atomic-close"
    )
    reply = await ticket_service.accept_operator_reply(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        source_chat_id=-100123,
        source_message_id=11,
        content="answer",
        media=None,
    )
    duplicate_reply = await ticket_service.accept_operator_reply(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        source_chat_id=-100123,
        source_message_id=11,
        content="answer",
        media=None,
    )
    assert reply.changed is True and reply.reopened is True
    assert duplicate_reply.changed is False

    async with ticket_service.database.session() as session:
        messages = list(
            (
                await session.scalars(
                    select(TicketMessage).where(TicketMessage.ticket_id == ticket.id)
                )
            ).all()
        )
        deliveries = list(
            (
                await session.scalars(
                    select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == ticket.id)
                )
            ).all()
        )
    assert len(messages) == 2
    assert len([job for job in deliveries if job.idempotency_key.startswith("copy:")]) == 2


async def test_missing_ticket_is_not_reported_as_duplicate(
    ticket_service: TicketService,
) -> None:
    with pytest.raises(TicketNotFoundError):
        await ticket_service.enqueue_text(
            ticket_id="00000000-0000-4000-8000-000000000001",
            direction=Direction.OPERATOR_TO_USER,
            text="must fail",
            target_chat_id=1,
            idempotency_key="missing-ticket-message",
        )


async def test_unexpected_integrity_error_is_not_reported_as_duplicate(
    ticket_service: TicketService,
) -> None:
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=19001, display_name="Constraint test", username=None
    )

    def fail_notification_insert(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "INSERT INTO notification_outbox" in statement:
            raise IntegrityError(statement, parameters, ValueError("simulated FK violation"))

    sync_engine = ticket_service.database.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", fail_notification_insert)
    try:
        with pytest.raises(IntegrityError, match="simulated FK violation"):
            await ticket_service.enqueue_notification(
                ticket_id=ticket.id,
                event_type="constraint-test",
                destination="test",
                recipient_identity_provider="telegram",
                recipient_identity_value=str(ticket.telegram_user_id),
                payload={},
                idempotency_key="notification:unexpected-integrity",
            )
    finally:
        event.remove(sync_engine, "before_cursor_execute", fail_notification_insert)
