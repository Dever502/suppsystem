from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import case, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportbot.api_idempotency import (
    ApiIdempotencyCommand,
    api_action_payload,
    load_api_replay_response,
)
from supportbot.audit import record_event
from supportbot.database import Database, retry_sqlite_locks
from supportbot.durable_work import enqueue_topic_reconciliation
from supportbot.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    NotificationOutbox,
    OperatorAction,
    Ticket,
    TicketMessage,
    TicketStatus,
    utcnow,
)
from supportbot.service_types import TicketNotFoundError, TicketView
from supportbot.trace import get_trace_id


@dataclass(frozen=True)
class OperatorMessageResult:
    """Committed result of an operator-to-user message command."""

    changed: bool
    reopened: bool = False
    ticket: TicketView | None = None


class TicketMessageService:
    """Atomic message persistence and outbox enqueue operations."""

    database: Database

    async def _is_blocked_in_session(self, session: AsyncSession, telegram_user_id: int) -> bool:
        raise NotImplementedError

    async def _ticket_view(self, session: AsyncSession, ticket: Ticket) -> TicketView:
        raise NotImplementedError

    async def _operator_message_replay(
        self, session: AsyncSession, command: ApiIdempotencyCommand | None
    ) -> OperatorMessageResult | None:
        response = await load_api_replay_response(session, command)
        if response is None:
            return None
        ticket_view: TicketView | None = None
        ticket_id = response.get("ticket_id")
        if isinstance(ticket_id, str):
            ticket = await session.get(Ticket, ticket_id)
            if ticket is not None:
                ticket_view = await self._ticket_view(session, ticket)
        return OperatorMessageResult(
            changed=bool(response["changed"]),
            reopened=response.get("reopened") is True,
            ticket=ticket_view,
        )

    async def add_internal_note(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        note: str,
        source_chat_id: int,
        source_message_id: int,
        idempotency_key: str,
    ) -> bool:
        async with self.database.session() as session:
            if await session.scalar(
                select(OperatorAction.id).where(OperatorAction.idempotency_key == idempotency_key)
            ):
                return False
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            ticket.last_activity_at = utcnow()
            session.add(
                TicketMessage(
                    ticket_id=ticket.id,
                    direction=Direction.OPERATOR_TO_USER,
                    channel="internal_note",
                    content=note,
                    media={"operator_telegram_id": operator_telegram_id},
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                )
            )
            session.add(
                OperatorAction(
                    ticket_id=ticket.id,
                    operator_telegram_id=operator_telegram_id,
                    action="add_internal_note",
                    idempotency_key=idempotency_key,
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(
                    select(OperatorAction.id).where(
                        OperatorAction.idempotency_key == idempotency_key
                    )
                ):
                    return False
                raise
            return True

    @retry_sqlite_locks
    async def enqueue_copy(
        self,
        *,
        ticket_id: str,
        direction: Direction,
        source_chat_id: int,
        source_message_id: int,
        target_chat_id: int,
        target_thread_id: int | None = None,
        content: str | None = None,
        media: dict[str, object] | None = None,
    ) -> bool:
        idempotency_key = f"copy:{direction.value}:{source_chat_id}:{source_message_id}"
        async with self.database.session() as session:
            if Direction(
                direction
            ) is Direction.OPERATOR_TO_USER and await self._is_blocked_in_session(
                session, target_chat_id
            ):
                return False
            if await session.scalar(
                select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == idempotency_key)
            ):
                return False
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            resolved_thread_id = target_thread_id
            if Direction(direction) is Direction.USER_TO_OPERATOR and resolved_thread_id is None:
                resolved_thread_id = ticket.topic_id
            payload: dict[str, object] = {
                "kind": "copy",
                "source_chat_id": source_chat_id,
                "source_message_id": source_message_id,
                "target_chat_id": target_chat_id,
                "target_thread_id": resolved_thread_id,
            }
            ticket.last_activity_at = utcnow()
            session.add(
                TicketMessage(
                    ticket_id=ticket_id,
                    direction=direction,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    content=content,
                    media=media,
                )
            )
            session.add(
                DeliveryOutbox(
                    ticket_id=ticket_id,
                    direction=direction,
                    idempotency_key=idempotency_key,
                    payload=payload,
                    status=(
                        DeliveryStatus.WAITING_TOPIC
                        if Direction(direction) is Direction.USER_TO_OPERATOR
                        and resolved_thread_id is None
                        else DeliveryStatus.PENDING
                    ),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(
                    select(DeliveryOutbox.id).where(
                        DeliveryOutbox.idempotency_key == idempotency_key
                    )
                ):
                    return False
                raise
            return True

    @retry_sqlite_locks
    async def enqueue_text(
        self,
        *,
        ticket_id: str,
        direction: Direction,
        text: str,
        target_chat_id: int,
        idempotency_key: str,
        channel: str = "api",
    ) -> bool:
        payload: dict[str, object] = {
            "kind": "send_text",
            "target_chat_id": target_chat_id,
            "text": text,
        }
        async with self.database.session() as session:
            if Direction(
                direction
            ) is Direction.OPERATOR_TO_USER and await self._is_blocked_in_session(
                session, target_chat_id
            ):
                return False
            if await session.scalar(
                select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == idempotency_key)
            ):
                return False
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            ticket.last_activity_at = utcnow()
            session.add(
                TicketMessage(
                    ticket_id=ticket_id,
                    direction=direction,
                    channel=channel,
                    source_chat_id=None,
                    source_message_id=None,
                    content=text,
                )
            )
            session.add(
                DeliveryOutbox(
                    ticket_id=ticket_id,
                    direction=direction,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(
                    select(DeliveryOutbox.id).where(
                        DeliveryOutbox.idempotency_key == idempotency_key
                    )
                ):
                    return False
                raise
            return True

    @retry_sqlite_locks
    async def send_operator_message(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        text: str,
        idempotency_key: str,
        reopen_idempotency_key: str,
        channel: str = "api",
        api_idempotency: ApiIdempotencyCommand | None = None,
    ) -> OperatorMessageResult:
        """Reopen when needed and enqueue an operator message in one transaction."""

        async with self.database.session() as session:
            if api_idempotency is not None and idempotency_key != api_idempotency.storage_key:
                raise ValueError("API idempotency storage key mismatch")
            replay = await self._operator_message_replay(session, api_idempotency)
            if replay is not None:
                return replay

            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            # On PostgreSQL another request may commit while this transaction
            # waits for the ticket row lock. Re-read the command only after the
            # lock is owned so a concurrent conflicting payload becomes 409.
            replay = await self._operator_message_replay(session, api_idempotency)
            if replay is not None:
                return replay

            initial_view = await self._ticket_view(session, ticket)
            if await self._is_blocked_in_session(session, initial_view.telegram_user_id):
                if api_idempotency is not None:
                    session.add(
                        OperatorAction(
                            ticket_id=ticket.id,
                            operator_telegram_id=operator_telegram_id,
                            action="send_ticket_message",
                            idempotency_key=idempotency_key,
                            payload=api_action_payload(
                                {"channel": channel},
                                command=api_idempotency,
                                changed=False,
                                response={"reopened": False, "ticket_id": ticket.id},
                            ),
                            result="completed",
                            trace_id=get_trace_id(),
                        )
                    )
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        replay = await self._operator_message_replay(session, api_idempotency)
                        if replay is None:
                            raise
                        return replay
                return OperatorMessageResult(changed=False)
            if await session.scalar(
                select(DeliveryOutbox.id).where(DeliveryOutbox.idempotency_key == idempotency_key)
            ):
                return OperatorMessageResult(changed=False)

            was_closed = TicketStatus(ticket.status) is TicketStatus.CLOSED
            if was_closed and await session.scalar(
                select(OperatorAction.id).where(
                    OperatorAction.idempotency_key == reopen_idempotency_key
                )
            ):
                return OperatorMessageResult(changed=False)
            if await session.scalar(
                select(OperatorAction.id).where(OperatorAction.idempotency_key == idempotency_key)
            ):
                return OperatorMessageResult(changed=False)

            transition_at = utcnow()
            reopened = False
            if was_closed:
                reopen_result = await session.execute(
                    update(Ticket)
                    .where(
                        Ticket.id == ticket.id,
                        Ticket.status == TicketStatus.CLOSED,
                    )
                    .values(
                        status=case(
                            (Ticket.topic_id.is_not(None), TicketStatus.OPEN),
                            else_=TicketStatus.PROVISIONING,
                        ),
                        closed_at=None,
                        last_activity_at=transition_at,
                    )
                )
                reopened = cast(CursorResult[object], reopen_result).rowcount == 1
            if not reopened:
                # SQLite ignores FOR UPDATE. If another distinct command won
                # the conditional reopen, this transaction still owns its
                # message but must not claim the reopen audit transition.
                if was_closed:
                    await session.refresh(ticket)
                ticket.last_activity_at = transition_at
            if reopened:
                session.add(
                    OperatorAction(
                        ticket_id=ticket.id,
                        operator_telegram_id=operator_telegram_id,
                        action="reopen_ticket",
                        idempotency_key=reopen_idempotency_key,
                        result="completed",
                        trace_id=get_trace_id(),
                    )
                )

            session.add(
                OperatorAction(
                    ticket_id=ticket.id,
                    operator_telegram_id=operator_telegram_id,
                    action="send_ticket_message",
                    idempotency_key=idempotency_key,
                    payload=api_action_payload(
                        {"channel": channel},
                        command=api_idempotency,
                        changed=True,
                        response={"reopened": reopened, "ticket_id": ticket.id},
                    ),
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            session.add(
                TicketMessage(
                    ticket_id=ticket.id,
                    direction=Direction.OPERATOR_TO_USER,
                    channel=channel,
                    source_chat_id=None,
                    source_message_id=None,
                    content=text,
                )
            )
            session.add(
                DeliveryOutbox(
                    ticket_id=ticket.id,
                    direction=Direction.OPERATOR_TO_USER,
                    idempotency_key=idempotency_key,
                    payload={
                        "kind": "send_text",
                        "target_chat_id": initial_view.telegram_user_id,
                        "text": text,
                    },
                )
            )
            if reopened:
                await enqueue_topic_reconciliation(
                    session, ticket_id=ticket.id, desired_status=TicketStatus.OPEN.value
                )
            try:
                await session.flush()
                await session.refresh(ticket)
                committed_view = await self._ticket_view(session, ticket)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await self._operator_message_replay(session, api_idempotency)
                if replay is not None:
                    return replay
                # A concurrent command with the same key is the only integrity
                # failure that represents a successful idempotent no-op. Do not
                # hide unrelated database failures as ``changed=False``: the API
                # must surface them so the caller can safely retry the whole
                # atomic command.
                duplicate_delivery = await session.scalar(
                    select(DeliveryOutbox.id).where(
                        DeliveryOutbox.idempotency_key == idempotency_key
                    )
                )
                if duplicate_delivery is not None:
                    return OperatorMessageResult(changed=False)
                raise

        if reopened:
            record_event(
                "ticket_reopened",
                ticket_id=ticket_id,
                operator_telegram_id=operator_telegram_id,
            )
        record_event(
            "operator_message_enqueued",
            ticket_id=ticket_id,
            operator_telegram_id=operator_telegram_id,
        )
        return OperatorMessageResult(
            changed=True,
            reopened=reopened,
            ticket=committed_view,
        )

    @retry_sqlite_locks
    async def enqueue_notification(
        self,
        *,
        ticket_id: str,
        event_type: str,
        destination: str,
        recipient_identity_provider: str,
        recipient_identity_value: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> bool:
        async with self.database.session() as session:
            if await session.scalar(
                select(NotificationOutbox.id).where(
                    NotificationOutbox.idempotency_key == idempotency_key
                )
            ):
                return False
            if await session.get(Ticket, ticket_id) is None:
                raise TicketNotFoundError(ticket_id)
            session.add(
                NotificationOutbox(
                    ticket_id=ticket_id,
                    event_type=event_type,
                    destination=destination,
                    recipient_identity_provider=recipient_identity_provider,
                    recipient_identity_value=recipient_identity_value,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(
                    select(NotificationOutbox.id).where(
                        NotificationOutbox.idempotency_key == idempotency_key
                    )
                ):
                    return False
                raise
            return True
