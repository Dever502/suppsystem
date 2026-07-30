from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from suppsystem.database import Database
from suppsystem.models import (
    BlocklistEntry,
    DeliveryOutbox,
    OperatorAction,
    Ticket,
    TicketStatus,
    User,
    UserIdentity,
)
from suppsystem.outbox_repository import OutboxRepository
from suppsystem.service_types import TicketNotFoundError, TicketView


class TicketServiceBase:
    """Shared persistence primitives for ticket command and query operations."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.outbox = OutboxRepository(database)

    async def is_blocked(self, telegram_user_id: int) -> bool:
        async with self.database.session() as session:
            return await session.get(BlocklistEntry, telegram_user_id) is not None

    @staticmethod
    async def _is_blocked_in_session(session: AsyncSession, telegram_user_id: int) -> bool:
        return await session.get(BlocklistEntry, telegram_user_id) is not None

    @staticmethod
    async def _operator_action_exists(session: AsyncSession, idempotency_key: str) -> bool:
        return (
            await session.scalar(
                select(OperatorAction.id).where(OperatorAction.idempotency_key == idempotency_key)
            )
            is not None
        )

    @staticmethod
    async def _delivery_exists(session: AsyncSession, idempotency_key: str) -> bool:
        return (
            await session.scalar(
                select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == idempotency_key)
            )
            is not None
        )

    async def _get_or_create_telegram_user(
        self, telegram_user_id: int, display_name: str | None, username: str | None
    ) -> User:
        async with self.database.session() as session:
            statement = (
                select(UserIdentity)
                .options(selectinload(UserIdentity.user))
                .where(
                    UserIdentity.provider == "telegram",
                    UserIdentity.external_id == str(telegram_user_id),
                )
            )
            identity: UserIdentity | None = await session.scalar(statement)
            if identity is not None:
                identity.user.display_name = display_name
                identity.user.username = username
                await session.commit()
                return identity.user

            user = User(display_name=display_name, username=username)
            user.identities.append(
                UserIdentity(provider="telegram", external_id=str(telegram_user_id))
            )
            session.add(user)
            try:
                await session.commit()
                return user
            except IntegrityError:
                await session.rollback()
                identity = await session.scalar(statement)
                if identity is None:
                    raise
                return identity.user

    async def _ticket_view(self, session: AsyncSession, ticket: Ticket) -> TicketView:
        user = await session.get(User, ticket.user_id)
        if user is None:
            raise TicketNotFoundError(ticket.id)
        identity = await session.scalar(
            select(UserIdentity).where(
                UserIdentity.user_id == user.id,
                UserIdentity.provider == "telegram",
            )
        )
        if identity is None:
            raise TicketNotFoundError(ticket.id)
        return self._ticket_view_from_user(ticket, user, int(identity.external_id))

    @staticmethod
    def _ticket_view_from_user(
        ticket: Ticket,
        user: User,
        telegram_user_id: int,
        *,
        reopened: bool = False,
    ) -> TicketView:
        return TicketView(
            id=ticket.id,
            user_id=user.id,
            telegram_user_id=telegram_user_id,
            display_name=user.display_name,
            username=user.username,
            topic_id=ticket.topic_id,
            status=TicketStatus(ticket.status),
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            last_activity_at=ticket.last_activity_at,
            closed_at=ticket.closed_at,
            close_cycle=ticket.close_cycle,
            reopened=reopened,
        )

    @staticmethod
    def _loaded_ticket_view(ticket: Ticket) -> TicketView:
        user = ticket.user
        identity = next(
            (item for item in user.identities if item.provider == "telegram"),
            None,
        )
        if identity is None:
            raise TicketNotFoundError(ticket.id)
        return TicketServiceBase._ticket_view_from_user(
            ticket,
            user,
            int(identity.external_id),
        )
