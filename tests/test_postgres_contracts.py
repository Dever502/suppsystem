from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import event, func, select

from suppsystem.api import create_app
from suppsystem.api_idempotency import api_idempotency_command
from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.durable_work import DurableWorkRepository
from suppsystem.media_storage import StoredMedia
from suppsystem.migrations import upgrade_database
from suppsystem.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    InboundUpdate,
    OperatorAction,
    Ticket,
    TicketMessage,
    TicketStatus,
    UserIdentity,
    WorkStatus,
    utcnow,
)
from suppsystem.service_types import DeliveryJob, TicketView, TopicProvisioningConflictError
from suppsystem.services import TicketService
from suppsystem.web_models import MediaAsset

pytestmark = pytest.mark.postgres

API_TOKEN = "0123456789abcdef0123456789abcdef"


class ForcedOutboxInsertError(RuntimeError):
    """Test-only failure injected between the ticket transition and outbox commit."""


@asynccontextmanager
async def migrated_service(database_url: str) -> AsyncIterator[tuple[Database, TicketService]]:
    await upgrade_database(database_url)
    database = Database(database_url)
    try:
        yield database, TicketService(database)
    finally:
        await database.dispose()


async def open_ticket_with_topic(
    service: TicketService,
    *,
    telegram_user_id: int,
    topic_id: int,
) -> TicketView:
    ticket = await service.open_or_reopen(
        telegram_user_id=telegram_user_id,
        display_name=f"PostgreSQL contract {telegram_user_id}",
        username=None,
    )
    token = await service.claim_topic_provisioning(ticket.id)
    assert token is not None
    return await service.attach_topic(ticket.id, topic_id, token=token)


async def load_delivery(database: Database, delivery_id: str) -> DeliveryOutbox:
    async with database.session() as session:
        delivery = await session.get(DeliveryOutbox, delivery_id)
        assert delivery is not None
        session.expunge(delivery)
        return delivery


async def make_delivery_stale(database: Database, delivery_id: str) -> None:
    async with database.session() as session:
        delivery = await session.get(DeliveryOutbox, delivery_id)
        assert delivery is not None
        delivery.claimed_at = utcnow() - timedelta(seconds=301)
        await session.commit()


async def enqueue_text_and_claim(
    service: TicketService,
    *,
    telegram_user_id: int,
    idempotency_key: str,
) -> DeliveryJob:
    ticket = await service.open_or_reopen(
        telegram_user_id=telegram_user_id,
        display_name="PostgreSQL delivery claim",
        username=None,
    )
    assert await service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="claim me",
        target_chat_id=telegram_user_id,
        idempotency_key=idempotency_key,
    )
    jobs = await service.outbox.claim_due_deliveries()
    assert len(jobs) == 1
    return jobs[0]


async def operator_message_effect_counts(
    database: Database,
    *,
    ticket_id: str,
    message_keys: Sequence[str],
    reopen_keys: Sequence[str],
) -> tuple[int, int, int, int]:
    async with database.session() as session:
        messages = await session.scalar(
            select(func.count())
            .select_from(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id, TicketMessage.channel == "api")
        )
        deliveries = await session.scalar(
            select(func.count())
            .select_from(DeliveryOutbox)
            .where(
                DeliveryOutbox.ticket_id == ticket_id,
                DeliveryOutbox.idempotency_key.in_(message_keys),
            )
        )
        send_actions = await session.scalar(
            select(func.count())
            .select_from(OperatorAction)
            .where(
                OperatorAction.ticket_id == ticket_id,
                OperatorAction.action == "send_ticket_message",
                OperatorAction.idempotency_key.in_(message_keys),
            )
        )
        reopen_actions = await session.scalar(
            select(func.count())
            .select_from(OperatorAction)
            .where(
                OperatorAction.ticket_id == ticket_id,
                OperatorAction.action == "reopen_ticket",
                OperatorAction.idempotency_key.in_(reopen_keys),
            )
        )
    return tuple(int(value or 0) for value in (messages, deliveries, send_actions, reopen_actions))


async def test_postgres_web_photo_persists_message_before_media(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        media = StoredMedia(
            id="d2a3f55e-a6de-4ff7-a55c-196a807325ab",
            storage_path="web-media/assets/d2/test.png",
            mime_type="image/png",
            size_bytes=67,
            sha256="a" * 64,
            original_filename="test.png",
        )
        result = await service.accept_message(
            identity_mode="external_id",
            external_user_id="postgres-photo-user",
            email="postgres-photo@example.com",
            display_name="PostgreSQL photo",
            remnawave_user_uuid=None,
            content="Screenshot",
            media=media,
            target_chat_id=-100123,
            command=api_idempotency_command(
                operation="web_message",
                resource="postgres-photo-user",
                key="postgres-web-photo",
                payload={
                    "external_user_id": "postgres-photo-user",
                    "email": "postgres-photo@example.com",
                    "text": "Screenshot",
                    "photo_sha256": media.sha256,
                },
            ),
        )

        async with database.session() as session:
            message = await session.get(TicketMessage, result.message_id)
            asset = await session.get(MediaAsset, media.id)
            delivery = await session.scalar(
                select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == result.ticket.id)
            )

        assert result.changed is True
        assert message is not None
        assert message.media is not None
        assert message.media["media_id"] == media.id
        assert asset is not None
        assert asset.message_id == result.message_id
        assert delivery is not None
        assert delivery.payload["kind"] == "send_photo"


@pytest.mark.parametrize("identity_mode", ["external_id", "email"])
async def test_postgres_web_boundary_values_fit_persisted_columns(
    postgres_database_url: str,
    identity_mode: str,
) -> None:
    long_email = "a" * (320 - len("@example.com")) + "@example.com"
    identity_value = "x" * 255 if identity_mode == "external_id" else long_email
    external_user_id = identity_value if identity_mode == "external_id" else None
    email = "boundary@example.com" if identity_mode == "external_id" else long_email
    command = api_idempotency_command(
        operation="web_message",
        resource=identity_value,
        key="k" * 128,
        payload={"identity_mode": identity_mode},
    )

    async with migrated_service(postgres_database_url) as (database, service):
        result = await service.accept_message(
            identity_mode=identity_mode,
            external_user_id=external_user_id,
            email=email,
            display_name="Boundary",
            remnawave_user_uuid=None,
            content="Boundary storage check",
            media=None,
            target_chat_id=-100123,
            command=command,
        )

        async with database.session() as session:
            identity = await session.scalar(
                select(UserIdentity).where(UserIdentity.external_id == identity_value)
            )
            action = await session.scalar(
                select(OperatorAction).where(OperatorAction.idempotency_key == command.storage_key)
            )
            delivery = await session.scalar(
                select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == result.ticket.id)
            )
        assert identity is not None
        assert action is not None
        assert delivery is not None
        assert len(delivery.idempotency_key) <= 512


async def test_postgres_inbound_retry_preserves_per_conversation_order(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, _service):
        repository = DurableWorkRepository(database)
        await repository.enqueue_inbound_update(
            7201, {"update_id": 7201}, ordering_key="chat:7:thread:21"
        )
        await repository.enqueue_inbound_update(
            7202, {"update_id": 7202}, ordering_key="chat:7:thread:21"
        )
        await repository.enqueue_inbound_update(
            7203, {"update_id": 7203}, ordering_key="chat:7:thread:22"
        )

        first = await repository.claim_inbound_update()
        assert first is not None and first.telegram_update_id == 7201
        assert await repository.retry_inbound_update(first, "temporary failure")

        independent = await repository.claim_inbound_update()
        assert independent is not None and independent.telegram_update_id == 7203
        assert await repository.finish_inbound_update(independent)
        assert await repository.claim_inbound_update() is None

        async with database.session() as session:
            retried_row = await session.get(InboundUpdate, 7201)
            assert retried_row is not None
            retried_row.next_attempt_at = utcnow()
            await session.commit()

        retried = await repository.claim_inbound_update()
        assert retried is not None and retried.telegram_update_id == 7201
        assert await repository.finish_inbound_update(retried)
        later = await repository.claim_inbound_update()
        assert later is not None and later.telegram_update_id == 7202
        assert await repository.finish_inbound_update(later)

        async with database.session() as session:
            statuses = list(
                (
                    await session.scalars(
                        select(InboundUpdate.status).order_by(InboundUpdate.telegram_update_id)
                    )
                ).all()
            )
        assert [WorkStatus(status) for status in statuses] == [WorkStatus.DELIVERED] * 3


async def test_postgres_operator_photo_persists_message_before_media(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        created = await service.accept_message(
            identity_mode="external_id",
            external_user_id="postgres-operator-photo-user",
            email="postgres-operator-photo@example.com",
            display_name="PostgreSQL operator photo",
            remnawave_user_uuid=None,
            content="Initial message",
            media=None,
            target_chat_id=-100123,
            command=api_idempotency_command(
                operation="web_message",
                resource="postgres-operator-photo-user",
                key="postgres-operator-photo-create",
                payload={"text": "Initial message"},
            ),
        )
        media = StoredMedia(
            id="e3b4f66f-b7ef-4008-b66d-207b918436bc",
            storage_path="web-media/assets/e3/operator.jpg",
            mime_type="image/jpeg",
            size_bytes=1024,
            sha256="b" * 64,
            original_filename=None,
        )

        result = await service.accept_operator_reply(
            ticket_id=created.ticket.id,
            operator_telegram_id=77,
            source_chat_id=-100123,
            source_message_id=91_001,
            content=None,
            media=media.message_metadata(),
            stored_media=media,
        )

        async with database.session() as session:
            message = await session.scalar(
                select(TicketMessage).where(
                    TicketMessage.ticket_id == created.ticket.id,
                    TicketMessage.direction == Direction.OPERATOR_TO_USER,
                )
            )
            asset = await session.get(MediaAsset, media.id)

        assert result.changed is True
        assert message is not None
        assert message.media is not None
        assert message.media["media_id"] == media.id
        assert asset is not None
        assert asset.message_id == message.id


async def test_postgres_concurrent_close_has_one_winner(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_001,
            topic_id=51_001,
        )

        results = await asyncio.wait_for(
            asyncio.gather(
                service.close(
                    ticket_id=ticket.id,
                    operator_telegram_id=101,
                    idempotency_key="postgres-concurrent-close-a",
                ),
                service.close(
                    ticket_id=ticket.id,
                    operator_telegram_id=102,
                    idempotency_key="postgres-concurrent-close-b",
                ),
            ),
            timeout=10,
        )

        async with database.session() as session:
            close_actions = await session.scalar(
                select(func.count())
                .select_from(OperatorAction)
                .where(
                    OperatorAction.ticket_id == ticket.id,
                    OperatorAction.action == "close_ticket",
                )
            )
        current = await service.get_ticket(ticket.id)

        assert sorted(results) == [False, True]
        assert close_actions == 1
        assert current.status is TicketStatus.CLOSED
        assert current.closed_at is not None
        assert current.close_cycle == 1


async def test_postgres_close_and_topic_attach_preserve_closed_state(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await service.open_or_reopen(
            telegram_user_id=41_002,
            display_name="PostgreSQL close attach",
            username=None,
        )
        token = await service.claim_topic_provisioning(ticket.id)
        assert token is not None

        async def attach_if_current() -> bool:
            try:
                await service.attach_topic(ticket.id, 51_002, token=token)
            except TopicProvisioningConflictError:
                return False
            return True

        close_changed, attach_changed = await asyncio.wait_for(
            asyncio.gather(
                service.close(
                    ticket_id=ticket.id,
                    operator_telegram_id=101,
                    idempotency_key="postgres-close-vs-attach",
                ),
                attach_if_current(),
            ),
            timeout=10,
        )

        current = await service.get_ticket(ticket.id)
        async with database.session() as session:
            persisted = await session.get(Ticket, ticket.id)
            assert persisted is not None

        assert close_changed is True
        assert current.status is TicketStatus.CLOSED
        assert current.closed_at is not None
        assert current.topic_id == (51_002 if attach_changed else None)
        assert persisted.topic_provisioning_token is None
        assert persisted.topic_provisioning_started_at is None


async def test_postgres_waiting_topic_restart_query_excludes_claimed_work(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (_database, service):
        ticket = await service.open_or_reopen(
            telegram_user_id=41_020,
            display_name="PostgreSQL waiting topic restart",
            username=None,
        )
        assert await service.enqueue_copy(
            ticket_id=ticket.id,
            direction=Direction.USER_TO_OPERATOR,
            source_chat_id=41_020,
            source_message_id=1,
            target_chat_id=-100123,
        )

        assert await service.list_waiting_topic_recovery_ticket_ids() == [ticket.id]
        assert await service.claim_topic_provisioning(ticket.id) is not None
        assert await service.list_waiting_topic_recovery_ticket_ids() == []
        assert await service.list_topic_provisioning_ticket_ids() == [ticket.id]


async def test_postgres_closed_topic_recovery_claim_is_exclusive(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (_database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_021,
            topic_id=51_021,
        )
        assert await service.enqueue_copy(
            ticket_id=ticket.id,
            direction=Direction.USER_TO_OPERATOR,
            source_chat_id=41_021,
            source_message_id=1,
            target_chat_id=-100123,
            target_thread_id=51_021,
        )
        assert await service.close(
            ticket_id=ticket.id,
            operator_telegram_id=101,
            idempotency_key="postgres-close-before-topic-recovery",
        )
        closed_before = await service.get_ticket(ticket.id)
        await service.invalidate_topic(ticket_id=ticket.id, topic_id=51_021)

        claims = await asyncio.wait_for(
            asyncio.gather(
                service.claim_closed_topic_recovery(ticket.id),
                service.claim_closed_topic_recovery(ticket.id),
            ),
            timeout=10,
        )
        tokens = [token for token in claims if token is not None]

        assert len(tokens) == 1
        attached = await service.attach_topic(ticket.id, 51_022, token=tokens[0])
        assert attached.status is TicketStatus.CLOSED
        assert attached.closed_at == closed_before.closed_at
        assert attached.topic_id == 51_022
        assert (
            await service.retarget_topic_deliveries(
                ticket_id=ticket.id,
                old_topic_id=51_021,
                new_topic_id=51_022,
            )
            == 1
        )
        jobs = await service.outbox.claim_due_deliveries()
        assert len(jobs) == 1
        assert jobs[0].payload["target_thread_id"] == 51_022


async def test_postgres_rating_remains_valid_during_reopen(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_003,
            topic_id=51_003,
        )
        assert await service.close(
            ticket_id=ticket.id,
            operator_telegram_id=101,
            idempotency_key="postgres-rating-close",
        )
        closed = await service.get_ticket(ticket.id)
        rating_key = "postgres-rating-race"
        reopen_key = "postgres-rating-reopen"

        rating_accepted, reopened = await asyncio.wait_for(
            asyncio.gather(
                service.enqueue_rating(
                    ticket_id=ticket.id,
                    source_chat_id=ticket.telegram_user_id,
                    score=5,
                    close_cycle=closed.close_cycle,
                    target_chat_id=-100_123,
                    text="PostgreSQL concurrent rating: 5/5",
                    idempotency_key=rating_key,
                ),
                service.reopen(
                    ticket_id=ticket.id,
                    operator_telegram_id=101,
                    idempotency_key=reopen_key,
                ),
            ),
            timeout=10,
        )

        async with database.session() as session:
            rating_messages = await session.scalar(
                select(func.count())
                .select_from(TicketMessage)
                .where(
                    TicketMessage.ticket_id == ticket.id,
                    TicketMessage.channel == "rating",
                )
            )
            rating_deliveries = await session.scalar(
                select(func.count())
                .select_from(DeliveryOutbox)
                .where(DeliveryOutbox.idempotency_key == rating_key)
            )
            reopen_actions = await session.scalar(
                select(func.count())
                .select_from(OperatorAction)
                .where(OperatorAction.idempotency_key == reopen_key)
            )
        current = await service.get_ticket(ticket.id)

        assert reopened is True
        assert rating_accepted is True
        assert current.status is TicketStatus.OPEN
        assert current.closed_at is None
        assert rating_messages == 1
        assert rating_deliveries == 1
        assert reopen_actions == 1


async def test_postgres_operator_message_failure_rolls_back_whole_command(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_004,
            topic_id=51_004,
        )
        assert await service.close(
            ticket_id=ticket.id,
            operator_telegram_id=101,
            idempotency_key="postgres-atomic-failure-close",
        )
        message_key = "postgres-atomic-failure-message"
        reopen_key = "postgres-atomic-failure-reopen"

        def fail_outbox_insert(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.casefold().split())
            if normalized.startswith("insert into delivery_outbox"):
                raise ForcedOutboxInsertError("forced delivery outbox insert failure")

        event.listen(database.engine.sync_engine, "before_cursor_execute", fail_outbox_insert)
        try:
            with pytest.raises(ForcedOutboxInsertError):
                await service.send_operator_message(
                    ticket_id=ticket.id,
                    operator_telegram_id=101,
                    text="this transaction must roll back",
                    idempotency_key=message_key,
                    reopen_idempotency_key=reopen_key,
                )
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", fail_outbox_insert)

        current = await service.get_ticket(ticket.id)
        counts = await operator_message_effect_counts(
            database,
            ticket_id=ticket.id,
            message_keys=(message_key,),
            reopen_keys=(reopen_key,),
        )

        assert current.status is TicketStatus.CLOSED
        assert current.closed_at is not None
        assert counts == (0, 0, 0, 0)


async def test_postgres_concurrent_same_operator_message_key_commits_once(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_005,
            topic_id=51_005,
        )
        assert await service.close(
            ticket_id=ticket.id,
            operator_telegram_id=101,
            idempotency_key="postgres-same-message-close",
        )
        message_key = "postgres-same-message"
        reopen_key = "postgres-same-message-reopen"

        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    service.send_operator_message(
                        ticket_id=ticket.id,
                        operator_telegram_id=101,
                        text="one durable command",
                        idempotency_key=message_key,
                        reopen_idempotency_key=reopen_key,
                    )
                    for _ in range(2)
                )
            ),
            timeout=10,
        )

        current = await service.get_ticket(ticket.id)
        counts = await operator_message_effect_counts(
            database,
            ticket_id=ticket.id,
            message_keys=(message_key,),
            reopen_keys=(reopen_key,),
        )

        assert sorted(result.changed for result in results) == [False, True]
        assert sum(result.reopened for result in results) == 1
        assert current.status is TicketStatus.OPEN
        assert counts == (1, 1, 1, 1)


async def test_postgres_api_rejects_concurrent_same_key_different_payload(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_015,
            topic_id=51_015,
        )
        settings = Settings(
            support_bot_token=SecretStr("test-token"),
            support_group_id=-100123,
            api_enabled=True,
            api_admin_token=SecretStr(API_TOKEN),
        )
        app = create_app(database=database, ticket_service=service, settings=settings)
        headers = {
            "X-API-Token": API_TOKEN,
            "X-Idempotency-Key": "postgres-api-payload-conflict",
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.wait_for(
                asyncio.gather(
                    client.post(
                        f"/api/v1/tickets/{ticket.id}/messages",
                        json={"text": "first payload"},
                        headers=headers,
                    ),
                    client.post(
                        f"/api/v1/tickets/{ticket.id}/messages",
                        json={"text": "conflicting payload"},
                        headers=headers,
                    ),
                ),
                timeout=10,
            )

        message_key = f"api:message:{ticket.id}:postgres-api-payload-conflict"
        counts = await operator_message_effect_counts(
            database,
            ticket_id=ticket.id,
            message_keys=(message_key,),
            reopen_keys=(),
        )

        assert sorted(response.status_code for response in responses) == [200, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert counts == (1, 1, 1, 0)


async def test_postgres_distinct_operator_messages_reopen_once_and_both_commit(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_006,
            topic_id=51_006,
        )
        assert await service.close(
            ticket_id=ticket.id,
            operator_telegram_id=101,
            idempotency_key="postgres-distinct-message-close",
        )
        message_keys = (
            "postgres-distinct-message-a",
            "postgres-distinct-message-b",
        )
        reopen_keys = (
            "postgres-distinct-message-reopen-a",
            "postgres-distinct-message-reopen-b",
        )

        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    service.send_operator_message(
                        ticket_id=ticket.id,
                        operator_telegram_id=101,
                        text=f"message {index}",
                        idempotency_key=message_key,
                        reopen_idempotency_key=reopen_key,
                    )
                    for index, (message_key, reopen_key) in enumerate(
                        zip(message_keys, reopen_keys, strict=True),
                        start=1,
                    )
                )
            ),
            timeout=10,
        )

        current = await service.get_ticket(ticket.id)
        counts = await operator_message_effect_counts(
            database,
            ticket_id=ticket.id,
            message_keys=message_keys,
            reopen_keys=reopen_keys,
        )

        assert all(result.changed for result in results)
        assert sum(result.reopened for result in results) == 1
        assert current.status is TicketStatus.OPEN
        assert counts == (2, 2, 2, 1)


async def test_postgres_retry_preserves_delivery_fifo(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await open_ticket_with_topic(
            service,
            telegram_user_id=41_007,
            topic_id=51_007,
        )
        for source_message_id in (1, 2):
            assert await service.enqueue_copy(
                ticket_id=ticket.id,
                direction=Direction.USER_TO_OPERATOR,
                source_chat_id=ticket.telegram_user_id,
                source_message_id=source_message_id,
                target_chat_id=-100_123,
                target_thread_id=ticket.topic_id,
            )

        first_batch = await service.outbox.claim_due_deliveries()
        assert len(first_batch) == 1
        assert first_batch[0].payload["source_message_id"] == 1
        first_token = first_batch[0].claim_token

        assert await service.outbox.mark_delivery_retry(
            first_batch[0].id,
            claim_token=first_token,
            error="temporary PostgreSQL failure",
            retry_after_seconds=60,
            max_attempts=8,
        )
        assert await service.outbox.claim_due_deliveries() == []

        async with database.session() as session:
            retriable = await session.get(DeliveryOutbox, first_batch[0].id)
            assert retriable is not None
            retriable.next_attempt_at = utcnow()
            await session.commit()

        retry_batch = await service.outbox.claim_due_deliveries()
        assert len(retry_batch) == 1
        assert retry_batch[0].id == first_batch[0].id
        assert retry_batch[0].claim_token != first_token
        assert await service.outbox.mark_delivery_delivered(
            retry_batch[0].id,
            claim_token=retry_batch[0].claim_token,
        )

        second_batch = await service.outbox.claim_due_deliveries()
        assert len(second_batch) == 1
        assert second_batch[0].payload["source_message_id"] == 2


async def test_postgres_concurrent_delivery_claim_has_one_owner(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        ticket = await service.open_or_reopen(
            telegram_user_id=41_008,
            display_name="PostgreSQL concurrent delivery claim",
            username=None,
        )
        assert await service.enqueue_text(
            ticket_id=ticket.id,
            direction=Direction.OPERATOR_TO_USER,
            text="one owner only",
            target_chat_id=ticket.telegram_user_id,
            idempotency_key="postgres-concurrent-delivery-claim",
        )

        first_batch, second_batch = await asyncio.wait_for(
            asyncio.gather(
                service.outbox.claim_due_deliveries(limit=1),
                service.outbox.claim_due_deliveries(limit=1),
            ),
            timeout=10,
        )
        claimed = [*first_batch, *second_batch]

        assert len(claimed) == 1
        persisted = await load_delivery(database, claimed[0].id)
        assert persisted.status == DeliveryStatus.PROCESSING
        assert persisted.claim_token == claimed[0].claim_token
        assert persisted.attempt_count == 1


async def test_postgres_late_worker_cannot_overwrite_reclaimed_delivery(
    postgres_database_url: str,
) -> None:
    async with migrated_service(postgres_database_url) as (database, service):
        stale_job = await enqueue_text_and_claim(
            service,
            telegram_user_id=41_009,
            idempotency_key="postgres-late-worker",
        )
        await make_delivery_stale(database, stale_job.id)

        assert await service.outbox.release_stale_deliveries() == 1
        current_job = (await service.outbox.claim_due_deliveries())[0]
        assert current_job.claim_token != stale_job.claim_token
        assert current_job.attempt_count == 2

        assert not await service.outbox.mark_delivery_delivered(
            stale_job.id,
            claim_token=stale_job.claim_token,
            delivered_message_id=111,
        )
        processing = await load_delivery(database, stale_job.id)
        assert processing.status == DeliveryStatus.PROCESSING
        assert processing.claim_token == current_job.claim_token
        assert processing.delivered_message_id is None

        assert await service.outbox.mark_delivery_delivered(
            current_job.id,
            claim_token=current_job.claim_token,
            delivered_message_id=222,
        )
        delivered = await load_delivery(database, stale_job.id)
        assert delivered.status == DeliveryStatus.DELIVERED
        assert delivered.claim_token is None
        assert delivered.delivered_message_id == 222
        assert delivered.payload == {}


async def test_postgres_restart_recovers_persisted_stale_delivery_claim(
    postgres_database_url: str,
) -> None:
    await upgrade_database(postgres_database_url)
    first_database = Database(postgres_database_url)
    try:
        first_service = TicketService(first_database)
        stale_job = await enqueue_text_and_claim(
            first_service,
            telegram_user_id=41_010,
            idempotency_key="postgres-restart-claim",
        )
        await make_delivery_stale(first_database, stale_job.id)
    finally:
        await first_database.dispose()

    restarted_database = Database(postgres_database_url)
    try:
        restarted_service = TicketService(restarted_database)
        assert await restarted_service.outbox.release_stale_deliveries() == 1
        recovered = await restarted_service.outbox.claim_due_deliveries()

        assert len(recovered) == 1
        assert recovered[0].id == stale_job.id
        assert recovered[0].claim_token != stale_job.claim_token
        assert recovered[0].attempt_count == 2
    finally:
        await restarted_database.dispose()
