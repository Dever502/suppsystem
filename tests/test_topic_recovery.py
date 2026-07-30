from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CopyMessage
from pydantic import SecretStr
from sqlalchemy import select

from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.delivery import DeliveryWorker
from suppsystem.models import DeliveryOutbox, DeliveryStatus, Direction, TicketStatus
from suppsystem.services import TicketService
from suppsystem.telegram_adapter import TelegramSupportAdapter, TicketLockPool
from suppsystem.telegram_formatting import topic_name


class FakeLimiter:
    async def wait(self) -> None:
        return None

    async def defer(self, seconds: float) -> None:
        del seconds


class RecoveryBot:
    def __init__(
        self,
        *,
        replacement_topic_id: int,
        missing_topic_id: int | None = None,
        fail_topic_creation: bool = False,
        fail_customer_card: bool = False,
    ) -> None:
        self.replacement_topic_id = replacement_topic_id
        self.missing_topic_id = missing_topic_id
        self.fail_topic_creation = fail_topic_creation
        self.fail_customer_card = fail_customer_card
        self.created_topics: list[dict[str, object]] = []
        self.sent_messages: list[dict[str, object]] = []
        self.copied_messages: list[dict[str, object]] = []

    async def create_forum_topic(self, **kwargs: object) -> SimpleNamespace:
        self.created_topics.append(kwargs)
        if self.fail_topic_creation:
            raise RuntimeError("topic creation outcome is unknown")
        return SimpleNamespace(message_thread_id=self.replacement_topic_id)

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        self.sent_messages.append(kwargs)
        if self.fail_customer_card:
            raise RuntimeError("customer card failed after attachment")
        return SimpleNamespace(message_id=700)

    async def copy_message(self, **kwargs: object) -> SimpleNamespace:
        if kwargs.get("message_thread_id") == self.missing_topic_id:
            raise TelegramBadRequest(
                method=CopyMessage(chat_id=1, from_chat_id=2, message_id=3),
                message="message thread not found",
            )
        self.copied_messages.append(kwargs)
        return SimpleNamespace(message_id=800 + len(self.copied_messages))


def recovery_settings(tmp_path: Path) -> Settings:
    return Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        data_dir=tmp_path,
    )


def recovery_adapter(
    *, service: TicketService, bot: RecoveryBot, settings: Settings
) -> TelegramSupportAdapter:
    adapter = object.__new__(TelegramSupportAdapter)
    adapter.bot = bot  # type: ignore[assignment]
    adapter.ticket_service = service
    adapter.settings = settings
    adapter.limiter = FakeLimiter()  # type: ignore[assignment]
    adapter.panel_service = None
    adapter._ticket_locks = TicketLockPool()
    return adapter


async def attach_topic(service: TicketService, ticket_id: str, topic_id: int) -> None:
    token = await service.claim_topic_provisioning(ticket_id)
    assert token is not None
    await service.attach_topic(ticket_id, topic_id, token=token)


async def test_restart_recovers_unclaimed_waiting_delivery_after_partial_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/waiting-topic-restart.db"
    first_database = Database(database_url)
    await first_database.create_schema_for_tests()
    first_service = TicketService(first_database)
    ticket = await first_service.open_or_reopen(
        telegram_user_id=61_001,
        display_name="Restart recovery",
        username=None,
    )
    assert await first_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=61_001,
        source_message_id=1,
        target_chat_id=-100123,
    )
    assert await first_service.outbox.claim_due_deliveries() == []
    await first_database.dispose()

    restarted_database = Database(database_url)
    try:
        restarted_service = TicketService(restarted_database)
        bot = RecoveryBot(replacement_topic_id=61_101, fail_customer_card=True)
        adapter = recovery_adapter(
            service=restarted_service,
            bot=bot,
            settings=recovery_settings(tmp_path),
        )

        assert await restarted_service.list_waiting_topic_recovery_ticket_ids() == [ticket.id]
        with caplog.at_level(logging.INFO, logger="suppsystem.telegram_topic_manager"):
            assert await adapter.recover_waiting_topics_after_restart() == 1
        assert await adapter.recover_waiting_topics_after_restart() == 0

        events = [getattr(record, "event", None) for record in caplog.records]
        assert "startup_topic_recovery_partially_succeeded" in events
        assert "startup_topic_recovery_failed" not in events
        assert "topic_provisioning_uncertain" not in events

        recovered_ticket = await restarted_service.get_ticket(ticket.id)
        assert recovered_ticket.status is TicketStatus.OPEN
        assert recovered_ticket.topic_id == 61_101
        assert len(bot.created_topics) == 1

        async with restarted_database.session() as session:
            pending = await session.scalar(
                select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == ticket.id)
            )
            assert pending is not None
            assert DeliveryStatus(pending.status) is DeliveryStatus.PENDING
            assert pending.payload["target_thread_id"] == 61_101

        jobs = await restarted_service.outbox.claim_due_deliveries()
        assert len(jobs) == 1
        assert jobs[0].payload["target_thread_id"] == 61_101
        async with restarted_database.session() as session:
            persisted = await session.get(DeliveryOutbox, jobs[0].id)
            assert persisted is not None
            assert DeliveryStatus(persisted.status) is DeliveryStatus.PROCESSING
    finally:
        await restarted_database.dispose()


async def test_restart_never_retries_unknown_topic_creation_outcome(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/unknown-topic-restart.db"
    first_database = Database(database_url)
    await first_database.create_schema_for_tests()
    first_service = TicketService(first_database)
    ticket = await first_service.open_or_reopen(
        telegram_user_id=61_002,
        display_name="Unknown topic outcome",
        username=None,
    )
    assert await first_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=61_002,
        source_message_id=1,
        target_chat_id=-100123,
    )
    await first_database.dispose()

    restarted_database = Database(database_url)
    try:
        restarted_service = TicketService(restarted_database)
        bot = RecoveryBot(replacement_topic_id=61_102, fail_topic_creation=True)
        adapter = recovery_adapter(
            service=restarted_service,
            bot=bot,
            settings=recovery_settings(tmp_path),
        )

        assert await adapter.recover_waiting_topics_after_restart() == 0
        assert await adapter.recover_waiting_topics_after_restart() == 0
        assert len(bot.created_topics) == 1
        assert await restarted_service.list_waiting_topic_recovery_ticket_ids() == []
        assert await restarted_service.list_topic_provisioning_ticket_ids() == [ticket.id]
        assert await restarted_service.outbox.claim_due_deliveries() == []
    finally:
        await restarted_database.dispose()


async def test_restart_recovers_closed_waiting_delivery_without_reopening(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/closed-waiting-topic-restart.db"
    first_database = Database(database_url)
    await first_database.create_schema_for_tests()
    first_service = TicketService(first_database)
    ticket = await first_service.open_or_reopen(
        telegram_user_id=61_004,
        display_name="Closed restart recovery",
        username=None,
    )
    assert await first_service.enqueue_copy(
        ticket_id=ticket.id,
        direction=Direction.USER_TO_OPERATOR,
        source_chat_id=61_004,
        source_message_id=1,
        target_chat_id=-100123,
    )
    assert await first_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=42,
        idempotency_key="close-before-waiting-topic-restart",
    )
    closed_before = await first_service.get_ticket(ticket.id)
    await first_database.dispose()

    restarted_database = Database(database_url)
    try:
        restarted_service = TicketService(restarted_database)
        bot = RecoveryBot(replacement_topic_id=61_104)
        adapter = recovery_adapter(
            service=restarted_service,
            bot=bot,
            settings=recovery_settings(tmp_path),
        )

        assert await restarted_service.list_waiting_topic_recovery_ticket_ids() == [ticket.id]
        assert await adapter.recover_waiting_topics_after_restart() == 1

        recovered_ticket = await restarted_service.get_ticket(ticket.id)
        assert recovered_ticket.status is TicketStatus.CLOSED
        assert recovered_ticket.closed_at == closed_before.closed_at
        assert recovered_ticket.topic_id == 61_104
        assert len(bot.created_topics) == 1
        assert bot.created_topics[0]["name"] == topic_name(
            recovered_ticket,
            closed=True,
        )

        jobs = await restarted_service.outbox.claim_due_deliveries()
        assert len(jobs) == 1
        assert jobs[0].payload["target_thread_id"] == 61_104
    finally:
        await restarted_database.dispose()


async def test_deleted_topic_recovery_delivers_closed_ticket_queue_without_reopening(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/closed-topic-recovery.db")
    await database.create_schema_for_tests()
    try:
        service = TicketService(database)
        ticket = await service.open_or_reopen(
            telegram_user_id=61_003,
            display_name="Closed topic recovery",
            username=None,
        )
        await attach_topic(service, ticket.id, 61_200)
        for message_id in (1, 2):
            assert await service.enqueue_copy(
                ticket_id=ticket.id,
                direction=Direction.USER_TO_OPERATOR,
                source_chat_id=61_003,
                source_message_id=message_id,
                target_chat_id=-100123,
                target_thread_id=61_200,
            )
        assert await service.close(
            ticket_id=ticket.id,
            operator_telegram_id=42,
            idempotency_key="close-before-deleted-topic-recovery",
        )
        closed_before = await service.get_ticket(ticket.id)
        first_job = (await service.outbox.claim_due_deliveries())[0]

        bot = RecoveryBot(replacement_topic_id=61_201, missing_topic_id=61_200)
        settings = recovery_settings(tmp_path)
        adapter = recovery_adapter(service=service, bot=bot, settings=settings)
        worker = DeliveryWorker(
            bot=bot,  # type: ignore[arg-type]
            ticket_service=service,
            outbox=service.outbox,
            settings=settings,
            limiter=FakeLimiter(),  # type: ignore[arg-type]
            heartbeat_path=tmp_path / "delivery-worker-heartbeat",
            recover_missing_topic=adapter.recover_missing_topic,
        )

        await worker._deliver(first_job)

        recovered_ticket = await service.get_ticket(ticket.id)
        assert recovered_ticket.status is TicketStatus.CLOSED
        assert recovered_ticket.closed_at == closed_before.closed_at
        assert recovered_ticket.topic_id == 61_201
        assert len(bot.created_topics) == 1
        assert bot.created_topics[0]["name"] == topic_name(recovered_ticket, closed=True)

        for _ in range(2):
            jobs = await service.outbox.claim_due_deliveries()
            assert len(jobs) == 1
            assert jobs[0].payload["target_thread_id"] == 61_201
            await worker._deliver(jobs[0])
        assert await service.outbox.claim_due_deliveries() == []

        async with database.session() as session:
            deliveries = list(
                (
                    await session.scalars(
                        select(DeliveryOutbox)
                        .where(DeliveryOutbox.ticket_id == ticket.id)
                        .order_by(DeliveryOutbox.created_at, DeliveryOutbox.id)
                    )
                ).all()
            )
        assert [DeliveryStatus(delivery.status) for delivery in deliveries] == [
            DeliveryStatus.DELIVERED,
            DeliveryStatus.DELIVERED,
        ]
        assert len(bot.copied_messages) == 2
    finally:
        await database.dispose()
