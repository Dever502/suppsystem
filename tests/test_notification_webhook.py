from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.models import NotificationOutbox, NotificationStatus
from suppsystem.notification_webhook import NotificationWebhookWorker, parse_retry_after
from suppsystem.outbox_repository import OutboxRepository
from suppsystem.runtime_defaults import NOTIFICATION_WEBHOOK_MAX_ATTEMPTS
from suppsystem.services import NotificationJob, TicketService


class FakeTicketService:
    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.retries: list[dict[str, object]] = []
        self.failures: list[dict[str, str]] = []
        self.released = 0

    async def release_stale_notifications(self, stale_after_seconds: int = 300) -> int:
        self.released += 1
        return 0

    async def claim_due_notifications(self) -> list[NotificationJob]:
        return []

    async def mark_notification_delivered(self, notification_id: str, *, claim_token: str) -> bool:
        self.delivered.append(notification_id)
        return True

    async def mark_notification_retry(
        self,
        notification_id: str,
        *,
        claim_token: str,
        error: str,
        retry_after_seconds: float,
        max_attempts: int,
    ) -> bool:
        self.retries.append(
            {
                "notification_id": notification_id,
                "error": error,
                "retry_after_seconds": retry_after_seconds,
                "max_attempts": max_attempts,
            }
        )
        return True

    async def mark_notification_failed(
        self, notification_id: str, *, claim_token: str, error: str
    ) -> bool:
        self.failures.append({"notification_id": notification_id, "error": error})
        return True


def settings() -> Settings:
    return Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        notification_webhook_enabled=True,
        notification_webhook_url="https://receiver.example/support",
        notification_webhook_secret=SecretStr("0123456789abcdef0123456789abcdef"),
    )


def job() -> NotificationJob:
    return NotificationJob(
        id="notification-1",
        ticket_id="ticket-1",
        event_type="subscription_link_reissued",
        destination="subscription_owner",
        recipient_identity_provider="telegram",
        recipient_identity_value="123456789",
        payload={"subscription_url": "https://sub.example/new"},
        created_at=datetime(2026, 6, 26, tzinfo=UTC),
        attempt_count=1,
        claim_token="claim-1",
    )


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_worker_claims_one_notification_at_a_time_during_shutdown() -> None:
    class ClaimLimitService(FakeTicketService):
        def __init__(self) -> None:
            super().__init__()
            self.limits: list[int] = []
            self.worker: NotificationWebhookWorker | None = None

        async def claim_due_notifications(self, limit: int = 20) -> list[NotificationJob]:
            self.limits.append(limit)
            assert self.worker is not None
            self.worker.stop()
            return []

    service = ClaimLimitService()
    worker = NotificationWebhookWorker(
        outbox=service,  # type: ignore[arg-type]
        settings=settings(),
        client=client(lambda request: httpx.Response(204)),
    )
    service.worker = worker

    await worker.run()

    assert service.limits == [1]


@pytest.mark.asyncio
async def test_notification_webhook_posts_signed_payload() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(204)

    service = FakeTicketService()
    worker = NotificationWebhookWorker(
        outbox=service,
        settings=settings(),
        client=client(handler),
    )

    await worker._deliver(job())

    assert service.delivered == ["notification-1"]
    assert service.retries == []
    assert seen["url"] == "https://receiver.example/support"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-support-event-id"] == "notification-1"
    assert headers["x-support-signature"].startswith("sha256=")
    assert seen["payload"] == {
        "event_id": "notification-1",
        "event_type": "subscription_link_reissued",
        "ticket_id": "ticket-1",
        "destination": "subscription_owner",
        "recipient": {
            "identity_provider": "telegram",
            "identity_value": "123456789",
        },
        "payload": {"subscription_url": "https://sub.example/new"},
        "created_at": "2026-06-26T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_webhook_redelivers_same_event_after_local_ack_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[bytes] = []
    event_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        event_ids.append(request.headers["X-Support-Event-Id"])
        return httpx.Response(204)

    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    await database.create_schema_for_tests()
    try:
        ticket_service = TicketService(database)
        ticket = await ticket_service.open_or_reopen(
            telegram_user_id=123456789,
            display_name="Webhook user",
            username=None,
        )
        assert await ticket_service.enqueue_notification(
            ticket_id=ticket.id,
            event_type="subscription_link_reissued",
            destination="subscription_owner",
            recipient_identity_provider="telegram",
            recipient_identity_value=str(ticket.telegram_user_id),
            payload={"subscription_url": "https://sub.example/new"},
            idempotency_key="notification-reclaim",
        )
        outbox = OutboxRepository(database)
        first = (await outbox.claim_due_notifications())[0]

        real_commit = AsyncSession.commit
        fail_ack = True

        async def fail_first_commit(session: AsyncSession) -> None:
            nonlocal fail_ack
            if fail_ack:
                fail_ack = False
                raise RuntimeError("local ack commit failed")
            await real_commit(session)

        async with client(handler) as http_client:
            worker = NotificationWebhookWorker(
                outbox=outbox,
                settings=settings(),
                client=http_client,
            )
            monkeypatch.setattr(AsyncSession, "commit", fail_first_commit)
            with pytest.raises(RuntimeError, match="local ack commit failed"):
                await worker._deliver(first)
            monkeypatch.setattr(AsyncSession, "commit", real_commit)

            async with database.session() as session:
                pending_ack = await session.get(NotificationOutbox, first.id)
            assert pending_ack is not None
            assert pending_ack.status == NotificationStatus.PROCESSING
            assert pending_ack.claim_token == first.claim_token
            assert pending_ack.delivered_at is None

            assert await outbox.release_stale_notifications(stale_after_seconds=0) == 1
            second = (await outbox.claim_due_notifications())[0]
            assert second.id == first.id
            assert second.claim_token != first.claim_token
            assert second.attempt_count == 2
            await worker._deliver(second)

        async with database.session() as session:
            delivered = await session.get(NotificationOutbox, first.id)
        assert delivered is not None
        assert delivered.status == NotificationStatus.DELIVERED
        assert delivered.claim_token is None
        assert delivered.attempt_count == 2
        assert bodies[0] == bodies[1]
        assert event_ids == [first.id, first.id]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_notification_webhook_retries_5xx() -> None:
    service = FakeTicketService()
    worker = NotificationWebhookWorker(
        outbox=service,
        settings=settings(),
        client=client(lambda request: httpx.Response(503)),
    )

    await worker._deliver(job())

    assert service.delivered == []
    assert service.retries[0]["notification_id"] == "notification-1"
    assert service.retries[0]["max_attempts"] == NOTIFICATION_WEBHOOK_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_notification_webhook_respects_retry_after() -> None:
    service = FakeTicketService()
    worker = NotificationWebhookWorker(
        outbox=service,
        settings=settings(),
        client=client(lambda request: httpx.Response(429, headers={"Retry-After": "120"})),
    )

    await worker._deliver(job())

    assert service.retries[0]["retry_after_seconds"] == 120


def test_retry_after_parses_http_date_and_rejects_invalid_value() -> None:
    now = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)

    assert parse_retry_after("Tue, 07 Jul 2026 12:02:00 GMT", now=now) == 120
    assert parse_retry_after("invalid", now=now) is None


@pytest.mark.asyncio
async def test_notification_webhook_marks_4xx_failed() -> None:
    service = FakeTicketService()
    worker = NotificationWebhookWorker(
        outbox=service,
        settings=settings(),
        client=client(lambda request: httpx.Response(400)),
    )

    await worker._deliver(job())

    assert service.delivered == []
    assert service.retries == []
    assert service.failures == [
        {
            "notification_id": "notification-1",
            "error": "notification webhook returned HTTP 400",
        }
    ]
