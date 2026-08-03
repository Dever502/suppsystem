from __future__ import annotations

import logging
import uuid
from typing import cast

from sqlalchemy import case, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from suppsystem.api_idempotency import (
    ApiIdempotencyCommand,
    api_action_payload,
    load_api_replay_response,
)
from suppsystem.durable_work import enqueue_topic_reconciliation
from suppsystem.media_storage import StoredMedia
from suppsystem.models import (
    DeliveryOutbox,
    Direction,
    OperatorAction,
    Ticket,
    TicketChannel,
    TicketMessage,
    TicketStatus,
    utcnow,
)
from suppsystem.service_types import TicketNotFoundError
from suppsystem.ticket_ingress_service import TelegramOperatorReplyResult, TicketIngressService
from suppsystem.ticket_message_service import OperatorMessageResult, TicketMessageService
from suppsystem.trace import get_trace_id
from suppsystem.web_models import MediaAsset, SystemSetting, TicketLifecycleEvent
from suppsystem.web_support_service import WebSupportService

logger = logging.getLogger(__name__)


class ChannelAwareTicketService(WebSupportService):
    async def validate_web_identity_mode(self, identity_mode: str) -> None:
        async with self.database.session() as session:
            setting = await session.get(SystemSetting, "web_identity_mode")
        if setting is not None and setting.value != identity_mode:
            raise RuntimeError(
                "WEB_IDENTITY_MODE cannot change after Web identities exist; migrate explicitly"
            )

    async def accept_operator_reply(
        self,
        *,
        ticket_id: str,
        operator_telegram_id: int,
        source_chat_id: int,
        source_message_id: int,
        content: str | None,
        media: dict[str, object] | None,
        stored_media: StoredMedia | None = None,
    ) -> TelegramOperatorReplyResult:
        async with self.database.session() as channel_session:
            stored_channel = await channel_session.scalar(
                select(Ticket.channel).where(Ticket.id == ticket_id)
            )
        if stored_channel is None:
            raise TicketNotFoundError(ticket_id)
        if TicketChannel(stored_channel) is TicketChannel.TELEGRAM:
            return await TicketIngressService.accept_operator_reply(
                cast(TicketIngressService, self),
                ticket_id=ticket_id,
                operator_telegram_id=operator_telegram_id,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                content=content,
                media=media,
            )
        key = f"copy:{Direction.OPERATOR_TO_USER.value}:{source_chat_id}:{source_message_id}"
        reopen_key = f"telegram:reopen:{source_chat_id}:{source_message_id}"
        async with self.database.session() as session:
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            view = await self._ticket_view(session, ticket)
            if await self._is_ticket_blocked_in_session(session, ticket.id):
                return TelegramOperatorReplyResult(False, True, ticket=view)
            if await self._operator_action_exists(session, key) or await self._delivery_exists(
                session, key
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
                session.add(
                    TicketLifecycleEvent(
                        ticket_id=ticket.id,
                        event_type="reopened",
                        channel=TicketChannel(ticket.channel),
                        close_cycle=ticket.close_cycle,
                        created_at=now,
                    )
                )
            ticket.last_activity_at = now
            session.add(
                OperatorAction(
                    ticket_id=ticket.id,
                    operator_telegram_id=operator_telegram_id,
                    action="send_ticket_message",
                    idempotency_key=key,
                    payload={"channel": TicketChannel(ticket.channel).value},
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            message_id = uuid.uuid4()
            effective_media = stored_media.message_metadata() if stored_media else media
            session.add(
                TicketMessage(
                    id=str(message_id),
                    ticket_id=ticket.id,
                    direction=Direction.OPERATOR_TO_USER,
                    channel=TicketChannel(ticket.channel).value,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    content=content,
                    media=effective_media,
                )
            )
            if stored_media is not None:
                await session.flush()
                session.add(
                    MediaAsset(
                        id=stored_media.id,
                        ticket_id=ticket.id,
                        message_id=str(message_id),
                        storage_path=stored_media.storage_path,
                        mime_type=stored_media.mime_type,
                        size_bytes=stored_media.size_bytes,
                        sha256=stored_media.sha256,
                        original_filename=stored_media.original_filename,
                    )
                )
            if TicketChannel(ticket.channel) is TicketChannel.TELEGRAM:
                if view.telegram_user_id is None:
                    raise RuntimeError("Telegram ticket has no Telegram identity")
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
                if await self._operator_action_exists(session, key) or await self._delivery_exists(
                    session, key
                ):
                    return TelegramOperatorReplyResult(False, False)
                raise
        logger.info(
            "Accepted operator reply for Web support",
            extra={
                "event": "web_operator_reply_accepted",
                "ticket_id": ticket_id,
                "operator_telegram_id": operator_telegram_id,
            },
        )
        return TelegramOperatorReplyResult(True, False, reopened, committed)

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
        async with self.database.session() as channel_session:
            stored_channel = await channel_session.scalar(
                select(Ticket.channel).where(Ticket.id == ticket_id)
            )
        if stored_channel is None:
            raise TicketNotFoundError(ticket_id)
        if TicketChannel(stored_channel) is TicketChannel.TELEGRAM:
            return await TicketMessageService.send_operator_message(
                cast(TicketMessageService, self),
                ticket_id=ticket_id,
                operator_telegram_id=operator_telegram_id,
                text=text,
                idempotency_key=idempotency_key,
                reopen_idempotency_key=reopen_idempotency_key,
                channel=channel,
                api_idempotency=api_idempotency,
            )
        async with self.database.session() as session:
            if api_idempotency is not None and idempotency_key != api_idempotency.storage_key:
                raise ValueError("API idempotency storage key mismatch")
            replay = await load_api_replay_response(session, api_idempotency)
            if replay is not None:
                return OperatorMessageResult(
                    changed=bool(replay["changed"]),
                    reopened=bool(replay.get("reopened")),
                )
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            replay = await load_api_replay_response(session, api_idempotency)
            if replay is not None:
                return OperatorMessageResult(
                    changed=bool(replay["changed"]),
                    reopened=bool(replay.get("reopened")),
                )
            initial_view = await self._ticket_view(session, ticket)
            if await self._is_ticket_blocked_in_session(session, ticket.id):
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
                        replay = await load_api_replay_response(session, api_idempotency)
                        if replay is None:
                            raise
                        return OperatorMessageResult(
                            changed=bool(replay["changed"]),
                            reopened=bool(replay.get("reopened")),
                        )
                return OperatorMessageResult(changed=False)
            if await self._operator_action_exists(session, idempotency_key):
                return OperatorMessageResult(changed=False)

            now = utcnow()
            was_closed = TicketStatus(ticket.status) is TicketStatus.CLOSED
            reopened = False
            if was_closed:
                result = await session.execute(
                    update(Ticket)
                    .where(Ticket.id == ticket.id, Ticket.status == TicketStatus.CLOSED)
                    .values(
                        status=case(
                            (Ticket.topic_id.is_not(None), TicketStatus.OPEN),
                            else_=TicketStatus.PROVISIONING,
                        ),
                        closed_at=None,
                        last_activity_at=now,
                    )
                )
                reopened = cast(CursorResult[object], result).rowcount == 1
            if not reopened:
                ticket.last_activity_at = now
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
                    TicketLifecycleEvent(
                        ticket_id=ticket.id,
                        event_type="reopened",
                        channel=TicketChannel(ticket.channel),
                        close_cycle=ticket.close_cycle,
                        created_at=now,
                    )
                )
            response = {"reopened": reopened, "ticket_id": ticket.id}
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
                        response=response,
                    ),
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            session.add(
                TicketMessage(
                    ticket_id=ticket.id,
                    direction=Direction.OPERATOR_TO_USER,
                    channel=TicketChannel(ticket.channel).value,
                    content=text,
                )
            )
            if TicketChannel(ticket.channel) is TicketChannel.TELEGRAM:
                if initial_view.telegram_user_id is None:
                    raise RuntimeError("Telegram ticket has no Telegram identity")
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
                view = await self._ticket_view(session, ticket)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await load_api_replay_response(session, api_idempotency)
                if replay is not None:
                    return OperatorMessageResult(
                        changed=bool(replay["changed"]),
                        reopened=bool(replay.get("reopened")),
                    )
                if await self._operator_action_exists(session, idempotency_key):
                    return OperatorMessageResult(changed=False)
                raise
            logger.info(
                "Accepted API operator message for Web support",
                extra={
                    "event": "web_operator_api_message_accepted",
                    "ticket_id": ticket_id,
                    "operator_telegram_id": operator_telegram_id,
                },
            )
            return OperatorMessageResult(True, reopened, view)
