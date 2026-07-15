from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportbot.audit import record_event
from supportbot.database import retry_sqlite_locks
from supportbot.durable_work import enqueue_topic_reconciliation
from supportbot.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    OperatorAction,
    Ticket,
    TicketMessage,
    TicketStatus,
    User,
    UserIdentity,
    utcnow,
)
from supportbot.service_types import TicketNotFoundError, TicketView
from supportbot.ticket_service_base import TicketServiceBase
from supportbot.trace import get_trace_id


@dataclass(frozen=True)
class CustomerMessageResult:
    changed: bool
    blocked: bool
    ticket: TicketView | None = None
    reopened: bool = False


@dataclass(frozen=True)
class TelegramOperatorReplyResult:
    changed: bool
    blocked: bool
    reopened: bool = False
    ticket: TicketView | None = None


class TicketIngressService(TicketServiceBase):
    async def _ticket_for_telegram_id(
        self, session: AsyncSession, telegram_user_id: int
    ) -> Ticket | None:
        identity = await session.scalar(
            select(UserIdentity).where(
                UserIdentity.provider == "telegram",
                UserIdentity.external_id == str(telegram_user_id),
            )
        )
        if identity is None:
            return None
        return cast(
            Ticket | None,
            await session.scalar(select(Ticket).where(Ticket.user_id == identity.user_id)),
        )

    @retry_sqlite_locks
    async def accept_customer_message(
        self,
        *,
        telegram_user_id: int,
        display_name: str | None,
        username: str | None,
        source_chat_id: int,
        source_message_id: int,
        target_chat_id: int,
        content: str | None,
        media: dict[str, object] | None,
    ) -> CustomerMessageResult:
        key = f"copy:{Direction.USER_TO_OPERATOR.value}:{source_chat_id}:{source_message_id}"
        async with self.database.session() as session:
            if await session.scalar(
                select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == key)
            ):
                ticket = await self._ticket_for_telegram_id(session, telegram_user_id)
                if ticket is None:
                    raise RuntimeError("durable customer message has no ticket")
                return CustomerMessageResult(False, False, await self._ticket_view(session, ticket))
            if await self._is_blocked_in_session(session, telegram_user_id):
                return CustomerMessageResult(False, True)

            identity = await session.scalar(
                select(UserIdentity).where(
                    UserIdentity.provider == "telegram",
                    UserIdentity.external_id == str(telegram_user_id),
                )
            )
            user: User
            if identity is None:
                user = User(display_name=display_name, username=username)
                user.identities.append(
                    UserIdentity(provider="telegram", external_id=str(telegram_user_id))
                )
                session.add(user)
                await session.flush()
            else:
                existing_user = await session.get(User, identity.user_id)
                if existing_user is None:
                    raise RuntimeError("Telegram identity references a missing user")
                user = existing_user
                user.display_name = display_name
                user.username = username

            ticket = await session.scalar(
                select(Ticket).where(Ticket.user_id == user.id).with_for_update()
            )
            created = ticket is None
            reopened = False
            if ticket is None:
                ticket = Ticket(user_id=user.id, status=TicketStatus.PROVISIONING)
                session.add(ticket)
                await session.flush()
            elif TicketStatus(ticket.status) is TicketStatus.CLOSED:
                ticket.status = (
                    TicketStatus.OPEN if ticket.topic_id is not None else TicketStatus.PROVISIONING
                )
                ticket.closed_at = None
                ticket.last_activity_at = utcnow()
                reopened = True
            else:
                ticket.last_activity_at = utcnow()

            session.add(
                TicketMessage(
                    ticket_id=ticket.id,
                    direction=Direction.USER_TO_OPERATOR,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    content=content,
                    media=media,
                )
            )
            session.add(
                DeliveryOutbox(
                    ticket_id=ticket.id,
                    direction=Direction.USER_TO_OPERATOR,
                    idempotency_key=key,
                    payload={
                        "kind": "copy",
                        "source_chat_id": source_chat_id,
                        "source_message_id": source_message_id,
                        "target_chat_id": target_chat_id,
                        "target_thread_id": ticket.topic_id,
                    },
                    status=(
                        DeliveryStatus.PENDING
                        if ticket.topic_id is not None
                        else DeliveryStatus.WAITING_TOPIC
                    ),
                )
            )
            if created or reopened:
                await enqueue_topic_reconciliation(
                    session, ticket_id=ticket.id, desired_status=TicketStatus.OPEN.value
                )
            try:
                await session.flush()
                view = await self._ticket_view(session, ticket)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if not await session.scalar(
                    select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == key)
                ):
                    raise
                ticket = await self._ticket_for_telegram_id(session, telegram_user_id)
                if ticket is None:
                    raise RuntimeError("duplicate customer message has no ticket") from None
                return CustomerMessageResult(False, False, await self._ticket_view(session, ticket))
        if created or reopened:
            record_event("ticket_opened", ticket_id=view.id)
        return CustomerMessageResult(True, False, view, reopened)

    @retry_sqlite_locks
    async def accept_operator_reply(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        source_chat_id: int,
        source_message_id: int,
        content: str | None,
        media: dict[str, object] | None,
    ) -> TelegramOperatorReplyResult:
        key = f"copy:{Direction.OPERATOR_TO_USER.value}:{source_chat_id}:{source_message_id}"
        reopen_key = f"telegram:reopen:{source_chat_id}:{source_message_id}"
        async with self.database.session() as session:
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            view = await self._ticket_view(session, ticket)
            if await self._is_blocked_in_session(session, view.telegram_user_id):
                return TelegramOperatorReplyResult(False, True, ticket=view)
            if await session.scalar(
                select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == key)
            ):
                return TelegramOperatorReplyResult(False, False, ticket=view)

            reopened = TicketStatus(ticket.status) is TicketStatus.CLOSED
            now = utcnow()
            if reopened:
                ticket.status = (
                    TicketStatus.OPEN if ticket.topic_id is not None else TicketStatus.PROVISIONING
                )
                ticket.closed_at = None
                session.add(
                    OperatorAction(
                        ticket_id=ticket.id,
                        operator_telegram_id=operator_telegram_id,
                        action="reopen_ticket",
                        idempotency_key=reopen_key,
                        result="completed",
                        trace_id=get_trace_id(),
                    )
                )
            ticket.last_activity_at = now
            session.add(
                OperatorAction(
                    ticket_id=ticket.id,
                    operator_telegram_id=operator_telegram_id,
                    action="send_ticket_message",
                    idempotency_key=key,
                    payload={"channel": "telegram"},
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            session.add(
                TicketMessage(
                    ticket_id=ticket.id,
                    direction=Direction.OPERATOR_TO_USER,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    content=content,
                    media=media,
                )
            )
            session.add(
                DeliveryOutbox(
                    ticket_id=ticket.id,
                    direction=Direction.OPERATOR_TO_USER,
                    idempotency_key=key,
                    payload={
                        "kind": "copy",
                        "source_chat_id": source_chat_id,
                        "source_message_id": source_message_id,
                        "target_chat_id": view.telegram_user_id,
                    },
                )
            )
            if reopened:
                await enqueue_topic_reconciliation(
                    session, ticket_id=ticket.id, desired_status=TicketStatus.OPEN.value
                )
            try:
                await session.flush()
                committed = await self._ticket_view(session, ticket)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(
                    select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == key)
                ):
                    return TelegramOperatorReplyResult(False, False)
                raise
        if reopened:
            record_event(
                "ticket_reopened", ticket_id=ticket_id, operator_telegram_id=operator_telegram_id
            )
        record_event(
            "operator_message_enqueued",
            ticket_id=ticket_id,
            operator_telegram_id=operator_telegram_id,
        )
        return TelegramOperatorReplyResult(True, False, reopened, committed)
