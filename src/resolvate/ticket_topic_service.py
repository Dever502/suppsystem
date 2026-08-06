from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import case, exists, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from resolvate.audit import record_event
from resolvate.database import retry_sqlite_locks
from resolvate.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    Ticket,
    TicketChannel,
    TicketStatus,
    User,
    utcnow,
)
from resolvate.service_types import (
    TicketNotFoundError,
    TicketView,
    TopicAlreadyBoundError,
    TopicProvisioningConflictError,
)
from resolvate.ticket_service_base import TicketServiceBase


class TicketTopicService(TicketServiceBase):
    @retry_sqlite_locks
    async def open_or_reopen(
        self,
        *,
        telegram_user_id: int,
        display_name: str | None,
        username: str | None,
    ) -> TicketView:
        user = await self._get_or_create_telegram_user(telegram_user_id, display_name, username)
        async with self.database.session() as session:
            created = False
            reopened = False
            ticket = await session.scalar(
                select(Ticket).where(
                    Ticket.user_id == user.id,
                    Ticket.channel == TicketChannel.TELEGRAM,
                )
            )
            if ticket is None:
                ticket = Ticket(user_id=user.id, status=TicketStatus.PROVISIONING)
                session.add(ticket)
                try:
                    await session.commit()
                    created = True
                except IntegrityError:
                    await session.rollback()
                    ticket = await session.scalar(
                        select(Ticket).where(
                            Ticket.user_id == user.id,
                            Ticket.channel == TicketChannel.TELEGRAM,
                        )
                    )
                    if ticket is None:
                        raise

            if not created:
                reopen_result = await session.execute(
                    update(Ticket)
                    .where(Ticket.id == ticket.id, Ticket.status == TicketStatus.CLOSED)
                    .values(
                        status=case(
                            (Ticket.topic_id.is_not(None), TicketStatus.OPEN),
                            else_=TicketStatus.PROVISIONING,
                        ),
                        closed_at=None,
                        last_activity_at=utcnow(),
                    )
                )
                reopened = cast(CursorResult[object], reopen_result).rowcount == 1
                await session.commit()
                await session.refresh(ticket)

            if created or reopened:
                record_event("ticket_opened", ticket_id=ticket.id)
            return self._ticket_view_from_user(
                ticket,
                user,
                telegram_user_id,
                reopened=reopened,
            )

    @retry_sqlite_locks
    async def attach_topic(self, ticket_id: str, topic_id: int, *, token: str) -> TicketView:
        async with self.database.session() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            if ticket.topic_id is not None:
                if ticket.topic_id == topic_id:
                    # The exact attachment may be retried after its commit. A consumed
                    # token cannot authorize a different, late Telegram result.
                    return await self._ticket_view(session, ticket)
                raise TopicProvisioningConflictError(ticket_id)

            existing = await session.scalar(select(Ticket).where(Ticket.topic_id == topic_id))
            if existing is not None and existing.id != ticket.id:
                raise TopicAlreadyBoundError(topic_id)

            attach_result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.topic_id.is_(None),
                    Ticket.topic_provisioning_token == token,
                )
                .values(
                    topic_id=topic_id,
                    status=case(
                        (Ticket.status == TicketStatus.CLOSED, TicketStatus.CLOSED),
                        else_=TicketStatus.OPEN,
                    ),
                    closed_at=case(
                        (Ticket.status == TicketStatus.CLOSED, Ticket.closed_at),
                        else_=None,
                    ),
                    topic_provisioning_token=None,
                    topic_provisioning_started_at=None,
                )
            )
            if cast(CursorResult[object], attach_result).rowcount != 1:
                await session.rollback()
                current = await session.get(Ticket, ticket_id)
                if current is not None and current.topic_id == topic_id:
                    return await self._ticket_view(session, current)
                raise TopicProvisioningConflictError(ticket_id)

            waiting_deliveries = list(
                (
                    await session.scalars(
                        select(DeliveryOutbox).where(
                            DeliveryOutbox.ticket_id == ticket_id,
                            DeliveryOutbox.direction == Direction.USER_TO_OPERATOR,
                            DeliveryOutbox.status == DeliveryStatus.WAITING_TOPIC,
                        )
                    )
                ).all()
            )
            for delivery in waiting_deliveries:
                delivery.payload = {**delivery.payload, "target_thread_id": topic_id}
                delivery.status = DeliveryStatus.PENDING
                delivery.next_attempt_at = utcnow()
                delivery.claimed_at = None
                delivery.claim_token = None
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                existing = await session.scalar(select(Ticket).where(Ticket.topic_id == topic_id))
                if existing is not None and existing.id != ticket_id:
                    raise TopicAlreadyBoundError(topic_id) from error
                raise
            await session.refresh(ticket)
            record_event("topic_attached", ticket_id=ticket_id)
            return await self._ticket_view(session, ticket)

    async def _claim_topic(self, ticket_id: str, *conditions: ColumnElement[bool]) -> str | None:
        token = str(uuid.uuid4())
        async with self.database.session() as session:
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.topic_id.is_(None),
                    Ticket.topic_provisioning_token.is_(None),
                    *conditions,
                )
                .values(
                    topic_provisioning_token=token,
                    topic_provisioning_started_at=utcnow(),
                )
            )
            await session.commit()
            return token if cast(CursorResult[object], result).rowcount == 1 else None

    async def claim_topic_provisioning(self, ticket_id: str) -> str | None:
        return await self._claim_topic(ticket_id, Ticket.status != TicketStatus.CLOSED)

    async def claim_topic_reconciliation(self, ticket_id: str) -> str | None:
        """Claim replacement topic creation for any durable desired-state job."""

        return await self._claim_topic(ticket_id)

    async def claim_closed_topic_recovery(self, ticket_id: str) -> str | None:
        """Claim replacement-topic creation for a closed ticket with queued user traffic."""

        has_unfinished_user_delivery = exists(
            select(DeliveryOutbox.id)
            .where(
                DeliveryOutbox.ticket_id == Ticket.id,
                DeliveryOutbox.direction == Direction.USER_TO_OPERATOR,
                DeliveryOutbox.status.in_(
                    (
                        DeliveryStatus.WAITING_TOPIC,
                        DeliveryStatus.PENDING,
                        DeliveryStatus.PROCESSING,
                    )
                ),
            )
            .correlate(Ticket)
        )
        return await self._claim_topic(
            ticket_id,
            Ticket.status == TicketStatus.CLOSED,
            has_unfinished_user_delivery,
        )

    async def abort_topic_provisioning(self, *, ticket_id: str, token: str) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id, Ticket.topic_provisioning_token == token)
                .values(topic_provisioning_token=None, topic_provisioning_started_at=None)
            )
            await session.commit()

    async def get_ticket(self, ticket_id: str) -> TicketView:
        async with self.database.session() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            return await self._ticket_view(session, ticket)

    async def invalidate_topic(self, *, ticket_id: str, topic_id: int) -> None:
        async with self.database.session() as session:
            result = await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id, Ticket.topic_id == topic_id)
                .values(
                    topic_id=None,
                    status=case(
                        (Ticket.status == TicketStatus.CLOSED, TicketStatus.CLOSED),
                        else_=TicketStatus.PROVISIONING,
                    ),
                    closed_at=case(
                        (Ticket.status == TicketStatus.CLOSED, Ticket.closed_at),
                        else_=None,
                    ),
                    topic_provisioning_token=None,
                    topic_provisioning_started_at=None,
                )
            )
            await session.commit()
        if cast(CursorResult[object], result).rowcount == 1:
            record_event("topic_invalidated", ticket_id=ticket_id)

    async def retarget_topic_deliveries(
        self, *, ticket_id: str, old_topic_id: int, new_topic_id: int
    ) -> int:
        """Move unfinished user messages from a deleted topic to its replacement."""

        async with self.database.session() as session:
            entries = list(
                (
                    await session.scalars(
                        select(DeliveryOutbox).where(
                            DeliveryOutbox.ticket_id == ticket_id,
                            DeliveryOutbox.direction == Direction.USER_TO_OPERATOR,
                            DeliveryOutbox.status.in_(
                                (DeliveryStatus.PENDING, DeliveryStatus.PROCESSING)
                            ),
                        )
                    )
                ).all()
            )
            retargeted = 0
            for entry in entries:
                if entry.payload.get("target_thread_id") != old_topic_id:
                    continue
                entry.payload = {**entry.payload, "target_thread_id": new_topic_id}
                entry.status = DeliveryStatus.PENDING
                entry.attempt_count = 0
                entry.next_attempt_at = utcnow()
                entry.claimed_at = None
                entry.claim_token = None
                entry.last_error = None
                retargeted += 1
            await session.commit()
            return retargeted

    async def list_topic_provisioning_ticket_ids(self) -> list[str]:
        """Return unresolved topic claims for explicit operator recovery."""

        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(Ticket.id).where(
                            Ticket.topic_id.is_(None),
                            Ticket.topic_provisioning_token.is_not(None),
                        )
                    )
                ).all()
            )

    async def list_waiting_topic_recovery_ticket_ids(self) -> list[str]:
        """Return safely retryable topic work left unclaimed before a process stopped."""

        has_waiting_user_delivery = exists(
            select(DeliveryOutbox.id)
            .where(
                DeliveryOutbox.ticket_id == Ticket.id,
                DeliveryOutbox.direction == Direction.USER_TO_OPERATOR,
                DeliveryOutbox.status == DeliveryStatus.WAITING_TOPIC,
            )
            .correlate(Ticket)
        )
        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(Ticket.id)
                        .where(
                            Ticket.status.in_(
                                (
                                    TicketStatus.PROVISIONING,
                                    TicketStatus.OPEN,
                                    TicketStatus.CLOSED,
                                )
                            ),
                            Ticket.topic_id.is_(None),
                            Ticket.topic_provisioning_token.is_(None),
                            has_waiting_user_delivery,
                        )
                        .order_by(Ticket.created_at, Ticket.id)
                    )
                ).all()
            )

    async def reset_topic_provisioning(self, ticket_id: str) -> bool:
        """Allow an administrator to explicitly retry an uncertain topic creation."""

        async with self.database.session() as session:
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.topic_id.is_(None),
                    Ticket.topic_provisioning_token.is_not(None),
                )
                .values(topic_provisioning_token=None, topic_provisioning_started_at=None)
            )
            await session.commit()
            return cast(CursorResult[object], result).rowcount == 1

    async def get_by_topic(self, topic_id: int) -> TicketView | None:
        async with self.database.session() as session:
            ticket = await session.scalar(
                select(Ticket)
                .options(selectinload(Ticket.user).selectinload(User.identities))
                .where(Ticket.topic_id == topic_id)
            )
            return await self._ticket_view(session, ticket) if ticket else None
