from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import cast

from sqlalchemy import case, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from suppsystem.api_idempotency import (
    ApiIdempotencyCommand,
    api_action_payload,
    load_api_replay,
)
from suppsystem.audit import record_event
from suppsystem.database import retry_sqlite_locks
from suppsystem.durable_work import (
    enqueue_topic_reconciliation,
    enqueue_topic_reconciliations,
)
from suppsystem.models import (
    BlocklistEntry,
    DeliveryOutbox,
    Direction,
    OperatorAction,
    SupportBlock,
    Ticket,
    TicketChannel,
    TicketMessage,
    TicketStatus,
    User,
    UserIdentity,
    utcnow,
)
from suppsystem.service_types import (
    TicketNotFoundError,
    TicketView,
)
from suppsystem.ticket_topic_service import TicketTopicService
from suppsystem.trace import get_trace_id
from suppsystem.web_models import TicketLifecycleEvent


class TicketLifecycleService(TicketTopicService):
    @retry_sqlite_locks
    async def close(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        idempotency_key: str | None = None,
        notification_text: str | None = None,
        notification_target_chat_id: int | None = None,
        notification_idempotency_key: str | None = None,
        notification_reply_markup: dict[str, object] | None = None,
        notification_reply_markup_builder: Callable[[int], dict[str, object]] | None = None,
        notification_parse_mode: str | None = None,
        api_idempotency: ApiIdempotencyCommand | None = None,
    ) -> bool:
        action_key = idempotency_key or f"close:{ticket_id}:{uuid.uuid4()}"
        if api_idempotency is not None and action_key != api_idempotency.storage_key:
            raise ValueError("API idempotency storage key mismatch")
        notification_chat_id = notification_target_chat_id
        if notification_reply_markup is not None and notification_reply_markup_builder is not None:
            raise ValueError("notification reply markup inputs are mutually exclusive")

        async with self.database.session() as session:
            replay = await load_api_replay(session, api_idempotency)
            if replay is not None:
                return replay
            if await self._operator_action_exists(session, action_key):
                return False
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            transition_at = utcnow()
            close_cycle = await session.scalar(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.status != TicketStatus.CLOSED,
                )
                .values(
                    status=TicketStatus.CLOSED,
                    closed_at=transition_at,
                    last_activity_at=transition_at,
                    topic_provisioning_token=None,
                    topic_provisioning_started_at=None,
                )
                .values(close_cycle=Ticket.close_cycle + 1)
                .returning(Ticket.close_cycle)
            )
            if close_cycle is None:
                if api_idempotency is not None:
                    session.add(
                        OperatorAction(
                            ticket_id=ticket.id,
                            operator_telegram_id=operator_telegram_id,
                            action="close_ticket",
                            idempotency_key=action_key,
                            payload=api_action_payload({}, command=api_idempotency, changed=False),
                            result="completed",
                            trace_id=get_trace_id(),
                        )
                    )
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        replay = await load_api_replay(session, api_idempotency)
                        if replay is None:
                            raise
                        return replay
                else:
                    await session.rollback()
                return False
            session.add(
                OperatorAction(
                    ticket_id=ticket.id,
                    operator_telegram_id=operator_telegram_id,
                    action="close_ticket",
                    idempotency_key=action_key,
                    payload=api_action_payload({}, command=api_idempotency, changed=True),
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            session.add(
                TicketLifecycleEvent(
                    ticket_id=ticket.id,
                    event_type="closed",
                    channel=TicketChannel(ticket.channel),
                    close_cycle=close_cycle,
                    created_at=transition_at,
                )
            )
            notification_blocked = await self._is_ticket_blocked_in_session(session, ticket.id)
            if notification_text is not None and not notification_blocked:
                session.add(
                    TicketMessage(
                        ticket_id=ticket.id,
                        direction=Direction.OPERATOR_TO_USER,
                        channel="system",
                        source_chat_id=None,
                        source_message_id=None,
                        content=notification_text,
                    )
                )
                if notification_chat_id is not None:
                    delivery_key = notification_idempotency_key or f"{action_key}:notification"
                    reply_markup = (
                        notification_reply_markup_builder(close_cycle)
                        if notification_reply_markup_builder is not None
                        else notification_reply_markup
                    )
                    session.add(
                        DeliveryOutbox(
                            ticket_id=ticket.id,
                            direction=Direction.OPERATOR_TO_USER,
                            idempotency_key=delivery_key,
                            payload={
                                "kind": "send_text",
                                "target_chat_id": notification_chat_id,
                                "text": notification_text,
                                **(
                                    {"parse_mode": notification_parse_mode}
                                    if notification_parse_mode is not None
                                    else {}
                                ),
                                **(
                                    {"reply_markup": reply_markup}
                                    if reply_markup is not None
                                    else {}
                                ),
                            },
                        )
                    )
            await enqueue_topic_reconciliation(
                session, ticket_id=ticket.id, desired_status=TicketStatus.CLOSED.value
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await load_api_replay(session, api_idempotency)
                if replay is not None:
                    return replay
                if await self._operator_action_exists(session, action_key):
                    return False
                raise
        record_event(
            "ticket_closed", ticket_id=ticket_id, operator_telegram_id=operator_telegram_id
        )
        return True

    @retry_sqlite_locks
    async def enqueue_rating(
        self,
        *,
        ticket_id: str,
        source_chat_id: int,
        score: int,
        close_cycle: int,
        target_chat_id: int,
        text: str,
        idempotency_key: str,
        parse_mode: str | None = None,
    ) -> bool:
        if score not in range(1, 6):
            raise ValueError("score must be between 1 and 5")

        async with self.database.session() as session:
            if await self._delivery_exists(session, idempotency_key):
                return False
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            if close_cycle < 1 or close_cycle > ticket.close_cycle:
                await session.rollback()
                return False
            duplicate_rating = await session.scalar(
                select(TicketMessage.id).where(
                    TicketMessage.ticket_id == ticket_id,
                    TicketMessage.channel == "rating",
                    TicketMessage.rating_cycle == close_cycle,
                )
            )
            if duplicate_rating is not None:
                await session.rollback()
                return False
            ticket.last_activity_at = utcnow()
            session.add(
                TicketMessage(
                    ticket_id=ticket_id,
                    direction=Direction.USER_TO_OPERATOR,
                    channel="rating",
                    content=f"{score}/5",
                    media={"rating": score},
                    rating_cycle=close_cycle,
                    source_chat_id=source_chat_id,
                    source_message_id=None,
                )
            )
            session.add(
                TicketLifecycleEvent(
                    ticket_id=ticket.id,
                    event_type="rated",
                    channel=TicketChannel(ticket.channel),
                    close_cycle=close_cycle,
                )
            )
            session.add(
                DeliveryOutbox(
                    ticket_id=ticket_id,
                    direction=Direction.USER_TO_OPERATOR,
                    idempotency_key=idempotency_key,
                    payload={
                        "kind": "send_text",
                        "target_chat_id": target_chat_id,
                        "text": text,
                        **({"parse_mode": parse_mode} if parse_mode is not None else {}),
                    },
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                duplicate_delivery = await self._delivery_exists(session, idempotency_key)
                duplicate_rating = await session.scalar(
                    select(TicketMessage.id).where(
                        TicketMessage.ticket_id == ticket_id,
                        TicketMessage.rating_cycle == close_cycle,
                    )
                )
                if duplicate_delivery or duplicate_rating is not None:
                    return False
                raise
            return True

    @retry_sqlite_locks
    async def close_all(
        self, *, operator_telegram_id: int, idempotency_key: str
    ) -> list[TicketView]:
        async with self.database.session() as session:
            if await self._operator_action_exists(session, idempotency_key):
                return []
            session.add(
                OperatorAction(
                    operator_telegram_id=operator_telegram_id,
                    action="close_all",
                    idempotency_key=idempotency_key,
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                if await self._operator_action_exists(session, idempotency_key):
                    return []
                raise

            transition_at = utcnow()
            close_result = await session.execute(
                update(Ticket)
                .where(Ticket.status != TicketStatus.CLOSED)
                .values(
                    status=TicketStatus.CLOSED,
                    closed_at=transition_at,
                    last_activity_at=transition_at,
                    close_cycle=Ticket.close_cycle + 1,
                    topic_provisioning_token=None,
                    topic_provisioning_started_at=None,
                )
                .returning(Ticket.id, Ticket.channel, Ticket.close_cycle)
            )
            closed_rows = close_result.all()
            ticket_ids = [row.id for row in closed_rows]
            views: list[TicketView] = []
            if ticket_ids:
                trace_id = get_trace_id()
                session.add_all(
                    [
                        TicketLifecycleEvent(
                            ticket_id=row.id,
                            event_type="closed",
                            channel=TicketChannel(row.channel),
                            close_cycle=row.close_cycle,
                            created_at=transition_at,
                        )
                        for row in closed_rows
                    ]
                )
                session.add_all(
                    [
                        OperatorAction(
                            ticket_id=ticket_id,
                            operator_telegram_id=operator_telegram_id,
                            action="close_ticket",
                            idempotency_key=f"{idempotency_key}:{ticket_id}",
                            result="completed",
                            trace_id=trace_id,
                        )
                        for ticket_id in ticket_ids
                    ]
                )
                await enqueue_topic_reconciliations(
                    session,
                    ticket_ids=ticket_ids,
                    payload={"desired_status": TicketStatus.CLOSED.value},
                    next_attempt_at=transition_at,
                )
                tickets = list(
                    (
                        await session.scalars(
                            select(Ticket)
                            .options(joinedload(Ticket.user).selectinload(User.identities))
                            .where(Ticket.id.in_(ticket_ids))
                            .order_by(Ticket.id)
                        )
                    ).all()
                )
                views = [self._loaded_ticket_view(ticket) for ticket in tickets]
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await self._operator_action_exists(session, idempotency_key):
                    return []
                raise
        for view in views:
            record_event(
                "ticket_closed",
                ticket_id=view.id,
                operator_telegram_id=operator_telegram_id,
            )
        return views

    @retry_sqlite_locks
    async def block_ticket(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        reason: str | None = None,
        source: str = "telegram",
        idempotency_key: str | None = None,
        api_idempotency: ApiIdempotencyCommand | None = None,
    ) -> bool:
        action_key = idempotency_key or f"block-ticket:{ticket_id}:{uuid.uuid4()}"
        if api_idempotency is not None and action_key != api_idempotency.storage_key:
            raise ValueError("API idempotency storage key mismatch")
        async with self.database.session() as session:
            replay = await load_api_replay(session, api_idempotency)
            if replay is not None:
                return replay
            if await self._operator_action_exists(session, action_key):
                return False
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            block = await session.get(SupportBlock, ticket_id)
            telegram_user_id = (
                await session.scalar(
                    select(UserIdentity.external_id).where(
                        UserIdentity.user_id == ticket.user_id,
                        UserIdentity.provider == "telegram",
                    )
                )
                if TicketChannel(ticket.channel) is TicketChannel.TELEGRAM
                else None
            )
            legacy_entry = (
                await session.get(BlocklistEntry, int(telegram_user_id))
                if telegram_user_id is not None
                else None
            )
            changed = block is None and legacy_entry is None
            if block is None:
                session.add(
                    SupportBlock(
                        ticket_id=ticket_id,
                        blocked_by_telegram_id=operator_telegram_id,
                        reason=reason,
                        source=source,
                    )
                )
            if telegram_user_id is not None and legacy_entry is None:
                session.add(
                    BlocklistEntry(
                        telegram_user_id=int(telegram_user_id),
                        blocked_by_telegram_id=operator_telegram_id,
                        reason=reason,
                    )
                )
            session.add(
                OperatorAction(
                    ticket_id=ticket_id,
                    operator_telegram_id=operator_telegram_id,
                    action="block_user",
                    idempotency_key=action_key,
                    payload=api_action_payload(
                        {"reason": reason, "source": source},
                        command=api_idempotency,
                        changed=changed,
                    ),
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await load_api_replay(session, api_idempotency)
                if replay is not None:
                    return replay
                if await self._operator_action_exists(session, action_key):
                    return False
                raise
        record_event(
            "user_blocked",
            ticket_id=ticket_id,
            operator_telegram_id=operator_telegram_id,
        )
        return changed

    @retry_sqlite_locks
    async def unblock_ticket(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        source: str = "telegram",
        idempotency_key: str | None = None,
        api_idempotency: ApiIdempotencyCommand | None = None,
    ) -> bool:
        action_key = idempotency_key or f"unblock-ticket:{ticket_id}:{uuid.uuid4()}"
        if api_idempotency is not None and action_key != api_idempotency.storage_key:
            raise ValueError("API idempotency storage key mismatch")
        async with self.database.session() as session:
            replay = await load_api_replay(session, api_idempotency)
            if replay is not None:
                return replay
            if await self._operator_action_exists(session, action_key):
                return False
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            block = await session.get(SupportBlock, ticket_id)
            telegram_user_id = (
                await session.scalar(
                    select(UserIdentity.external_id).where(
                        UserIdentity.user_id == ticket.user_id,
                        UserIdentity.provider == "telegram",
                    )
                )
                if TicketChannel(ticket.channel) is TicketChannel.TELEGRAM
                else None
            )
            legacy_entry = (
                await session.get(BlocklistEntry, int(telegram_user_id))
                if telegram_user_id is not None
                else None
            )
            changed = block is not None or legacy_entry is not None
            if block is not None:
                await session.delete(block)
            if legacy_entry is not None:
                await session.delete(legacy_entry)
            session.add(
                OperatorAction(
                    ticket_id=ticket_id,
                    operator_telegram_id=operator_telegram_id,
                    action="unblock_user",
                    idempotency_key=action_key,
                    payload=api_action_payload(
                        {"source": source},
                        command=api_idempotency,
                        changed=changed,
                    ),
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await load_api_replay(session, api_idempotency)
                if replay is not None:
                    return replay
                if await self._operator_action_exists(session, action_key):
                    return False
                raise
        record_event(
            "user_unblocked",
            ticket_id=ticket_id,
            operator_telegram_id=operator_telegram_id,
        )
        return changed

    @retry_sqlite_locks
    async def block(
        self,
        *,
        telegram_user_id: int,
        operator_telegram_id: int,
        ticket_id: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        action_key = idempotency_key or f"block:{telegram_user_id}:{uuid.uuid4()}"
        async with self.database.session() as session:
            if await self._operator_action_exists(session, action_key):
                return False
            entry = await session.get(BlocklistEntry, telegram_user_id)
            if entry is None:
                session.add(
                    BlocklistEntry(
                        telegram_user_id=telegram_user_id,
                        blocked_by_telegram_id=operator_telegram_id,
                        reason=reason,
                    )
                )
            resolved_ticket_id = ticket_id or await session.scalar(
                select(Ticket.id)
                .join(UserIdentity, UserIdentity.user_id == Ticket.user_id)
                .where(
                    Ticket.channel == TicketChannel.TELEGRAM,
                    UserIdentity.provider == "telegram",
                    UserIdentity.external_id == str(telegram_user_id),
                )
            )
            if (
                resolved_ticket_id is not None
                and await session.get(SupportBlock, resolved_ticket_id) is None
            ):
                session.add(
                    SupportBlock(
                        ticket_id=resolved_ticket_id,
                        blocked_by_telegram_id=operator_telegram_id,
                        reason=reason,
                        source="legacy_service",
                    )
                )
            session.add(
                OperatorAction(
                    ticket_id=ticket_id,
                    operator_telegram_id=operator_telegram_id,
                    action="block_user",
                    idempotency_key=action_key,
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await self._operator_action_exists(session, action_key):
                    return False
                raise
        record_event(
            "user_blocked",
            ticket_id=ticket_id,
            operator_telegram_id=operator_telegram_id,
        )
        return entry is None

    @retry_sqlite_locks
    async def unblock(
        self,
        *,
        telegram_user_id: int,
        operator_telegram_id: int | None = None,
        ticket_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        action_key = idempotency_key or f"unblock:{telegram_user_id}:{uuid.uuid4()}"
        async with self.database.session() as session:
            if await self._operator_action_exists(session, action_key):
                return False
            entry = await session.get(BlocklistEntry, telegram_user_id)
            if entry is not None:
                await session.delete(entry)
            resolved_ticket_id = ticket_id or await session.scalar(
                select(Ticket.id)
                .join(UserIdentity, UserIdentity.user_id == Ticket.user_id)
                .where(
                    Ticket.channel == TicketChannel.TELEGRAM,
                    UserIdentity.provider == "telegram",
                    UserIdentity.external_id == str(telegram_user_id),
                )
            )
            if resolved_ticket_id is not None:
                support_block = await session.get(SupportBlock, resolved_ticket_id)
                if support_block is not None:
                    await session.delete(support_block)
            if operator_telegram_id is not None:
                session.add(
                    OperatorAction(
                        ticket_id=ticket_id,
                        operator_telegram_id=operator_telegram_id,
                        action="unblock_user",
                        idempotency_key=action_key,
                        result="completed",
                        trace_id=get_trace_id(),
                    )
                )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if operator_telegram_id is not None and await self._operator_action_exists(
                    session, action_key
                ):
                    return False
                raise
        if operator_telegram_id is not None:
            record_event(
                "user_unblocked",
                ticket_id=ticket_id,
                operator_telegram_id=operator_telegram_id,
            )
        return entry is not None

    async def list_tickets(
        self, *, status: TicketStatus | None, limit: int, offset: int
    ) -> list[TicketView]:
        async with self.database.session() as session:
            statement = (
                select(Ticket)
                .options(joinedload(Ticket.user).selectinload(User.identities))
                .order_by(Ticket.last_activity_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if status is not None:
                statement = statement.where(Ticket.status == status)
            tickets = list((await session.scalars(statement)).all())
            return [self._loaded_ticket_view(ticket) for ticket in tickets]

    @retry_sqlite_locks
    async def reopen(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        idempotency_key: str,
        api_idempotency: ApiIdempotencyCommand | None = None,
    ) -> bool:
        if api_idempotency is not None and idempotency_key != api_idempotency.storage_key:
            raise ValueError("API idempotency storage key mismatch")
        async with self.database.session() as session:
            replay = await load_api_replay(session, api_idempotency)
            if replay is not None:
                return replay
            if await self._operator_action_exists(session, idempotency_key):
                return False
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            transition_at = utcnow()
            reopen_result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
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
            if cast(CursorResult[object], reopen_result).rowcount != 1:
                if api_idempotency is not None:
                    session.add(
                        OperatorAction(
                            ticket_id=ticket.id,
                            operator_telegram_id=operator_telegram_id,
                            action="reopen_ticket",
                            idempotency_key=idempotency_key,
                            payload=api_action_payload({}, command=api_idempotency, changed=False),
                            result="completed",
                            trace_id=get_trace_id(),
                        )
                    )
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        replay = await load_api_replay(session, api_idempotency)
                        if replay is None:
                            raise
                        return replay
                else:
                    await session.rollback()
                return False
            session.add(
                OperatorAction(
                    ticket_id=ticket.id,
                    operator_telegram_id=operator_telegram_id,
                    action="reopen_ticket",
                    idempotency_key=idempotency_key,
                    payload=api_action_payload({}, command=api_idempotency, changed=True),
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            session.add(
                TicketLifecycleEvent(
                    ticket_id=ticket.id,
                    event_type="reopened",
                    channel=TicketChannel(ticket.channel),
                    close_cycle=ticket.close_cycle,
                    created_at=transition_at,
                )
            )
            await enqueue_topic_reconciliation(
                session, ticket_id=ticket.id, desired_status=TicketStatus.OPEN.value
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await load_api_replay(session, api_idempotency)
                if replay is not None:
                    return replay
                if await self._operator_action_exists(session, idempotency_key):
                    return False
                raise
        record_event(
            "ticket_reopened", ticket_id=ticket_id, operator_telegram_id=operator_telegram_id
        )
        return True

    async def queue_all_topic_reconciliations(self) -> int:
        async with self.database.session() as session:
            ticket_ids = list((await session.scalars(select(Ticket.id))).all())
            if not ticket_ids:
                return 0
            await enqueue_topic_reconciliations(
                session,
                ticket_ids=ticket_ids,
                payload={"reason": "bulk_sync"},
            )
            await session.commit()
            return len(ticket_ids)
