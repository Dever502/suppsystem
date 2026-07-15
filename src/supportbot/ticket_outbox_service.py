from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from supportbot.outbox_repository import OutboxRepository
from supportbot.service_types import DeliveryJob, NotificationJob


class HasOutbox(Protocol):
    outbox: OutboxRepository


class TicketOutboxService:
    """Worker-facing facade over the delivery and notification repositories."""

    outbox: OutboxRepository

    async def claim_due_notifications(self, limit: int = 20) -> list[NotificationJob]:
        return await self.outbox.claim_due_notifications(limit)

    async def mark_notification_delivered(self, notification_id: str, *, claim_token: str) -> bool:
        return await self.outbox.mark_notification_delivered(
            notification_id, claim_token=claim_token
        )

    async def mark_notification_retry(
        self,
        notification_id: str,
        *,
        claim_token: str,
        error: str,
        retry_after_seconds: float,
        max_attempts: int,
    ) -> bool:
        return await self.outbox.mark_notification_retry(
            notification_id,
            claim_token=claim_token,
            error=error,
            retry_after_seconds=retry_after_seconds,
            max_attempts=max_attempts,
        )

    async def mark_notification_failed(
        self, notification_id: str, *, claim_token: str, error: str
    ) -> bool:
        return await self.outbox.mark_notification_failed(
            notification_id, claim_token=claim_token, error=error
        )

    async def release_stale_notifications(self, stale_after_seconds: int = 300) -> int:
        return await self.outbox.release_stale_notifications(stale_after_seconds)

    async def claim_due_deliveries(self, limit: int = 20) -> list[DeliveryJob]:
        return await self.outbox.claim_due_deliveries(limit)

    async def mark_delivery_delivered(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        delivered_message_id: int | None = None,
    ) -> bool:
        return await self.outbox.mark_delivery_delivered(
            delivery_id,
            claim_token=claim_token,
            delivered_message_id=delivered_message_id,
        )

    async def mark_delivery_cancelled(
        self, delivery_id: str, *, claim_token: str, reason: str
    ) -> bool:
        return await self.outbox.mark_delivery_cancelled(
            delivery_id, claim_token=claim_token, reason=reason
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
        return await self.outbox.mark_delivery_retry(
            delivery_id,
            claim_token=claim_token,
            error=error,
            retry_after_seconds=retry_after_seconds,
            max_attempts=max_attempts,
        )

    async def release_stale_deliveries(self, stale_after_seconds: int = 300) -> int:
        return await self.outbox.release_stale_deliveries(stale_after_seconds)

    async def release_delivery_claims(self, claims: Sequence[tuple[str, str]]) -> int:
        return await self.outbox.release_delivery_claims(claims)
