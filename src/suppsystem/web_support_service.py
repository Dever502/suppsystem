from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from suppsystem.api_idempotency import (
    ApiIdempotencyCommand,
    api_action_payload,
    load_api_replay_response,
)
from suppsystem.database import retry_sqlite_locks
from suppsystem.durable_work import enqueue_topic_reconciliation
from suppsystem.media_storage import StoredMedia
from suppsystem.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    OperatorAction,
    Ticket,
    TicketChannel,
    TicketMessage,
    TicketStatus,
    User,
    UserIdentity,
    utcnow,
)
from suppsystem.service_types import TicketNotFoundError, TicketView
from suppsystem.telegram_formatting import ticket_topic_link
from suppsystem.ticket_service_base import TicketServiceBase
from suppsystem.trace import get_trace_id
from suppsystem.web_models import MediaAsset, SystemSetting, TicketLifecycleEvent

logger = logging.getLogger(__name__)
WEB_IDENTITY_MODE_KEY = "web_identity_mode"


@dataclass(frozen=True)
class WebMessageResult:
    changed: bool
    created: bool
    reopened: bool
    ticket: TicketView
    message_id: str
    message: WebMessageItem


@dataclass(frozen=True)
class WebMessageItem:
    id: str
    direction: Direction
    channel: str
    content: str | None
    created_at: datetime
    cursor: str
    media_id: str | None = None
    media_mime_type: str | None = None


@dataclass(frozen=True)
class WebMessagePage:
    items: list[WebMessageItem]
    next_cursor: str | None


def _message_item(message: TicketMessage) -> WebMessageItem:
    media_id = (
        str(message.media.get("media_id"))
        if isinstance(message.media, dict) and message.media.get("media_id")
        else None
    )
    media_mime_type = (
        str(message.media.get("mime_type"))
        if isinstance(message.media, dict) and message.media.get("mime_type")
        else None
    )
    return WebMessageItem(
        id=message.id,
        direction=Direction(message.direction),
        channel=message.channel,
        content=message.content,
        created_at=message.created_at,
        cursor=encode_cursor(message.created_at, message.id),
        media_id=media_id,
        media_mime_type=media_mime_type,
    )


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > 320
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
    ):
        raise ValueError("invalid email")
    local, domain = normalized.split("@", maxsplit=1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("invalid email")
    return normalized


def canonical_remnawave_uuid(value: str | None) -> str | None:
    if value is None:
        return None
    return str(uuid.UUID(value))


def encode_cursor(created_at: datetime, message_id: str) -> str:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    payload = json.dumps(
        [created_at.astimezone(UTC).isoformat(), message_id], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError
        created_at = datetime.fromisoformat(payload[0])
        message_id = str(uuid.UUID(payload[1]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at, message_id
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid cursor") from error


class WebSupportService(TicketServiceBase):
    async def ensure_web_identity_mode(self, identity_mode: str) -> None:
        for attempt in range(3):
            async with self.database.session() as session:
                setting = await session.get(SystemSetting, WEB_IDENTITY_MODE_KEY)
                if setting is not None:
                    if setting.value != identity_mode:
                        raise RuntimeError(
                            "WEB_IDENTITY_MODE cannot change after Web identities exist"
                        )
                    return
                session.add(SystemSetting(key=WEB_IDENTITY_MODE_KEY, value=identity_mode))
                try:
                    await session.commit()
                    return
                except IntegrityError:
                    await session.rollback()
                    if attempt == 2:
                        raise

    async def _load_replay(self, command: ApiIdempotencyCommand) -> WebMessageResult | None:
        async with self.database.session() as session:
            replay = await load_api_replay_response(session, command)
            if replay is None:
                return None
            ticket_id = replay.get("ticket_id")
            message_id = replay.get("message_id")
            if not isinstance(ticket_id, str) or not isinstance(message_id, str):
                raise RuntimeError("stored Web API response is malformed")
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise RuntimeError("stored Web API ticket is missing")
            message = await session.get(TicketMessage, message_id)
            if message is None or message.ticket_id != ticket.id:
                raise RuntimeError("stored Web API message is missing")
            return WebMessageResult(
                changed=bool(replay["changed"]),
                created=bool(replay.get("created")),
                reopened=bool(replay.get("reopened")),
                ticket=await self._ticket_view(session, ticket),
                message_id=message_id,
                message=_message_item(message),
            )

    @retry_sqlite_locks
    async def accept_message(
        self,
        *,
        identity_mode: str,
        external_user_id: str | None,
        email: str,
        display_name: str | None,
        remnawave_user_uuid: str | None,
        content: str | None,
        media: StoredMedia | None,
        target_chat_id: int,
        command: ApiIdempotencyCommand,
    ) -> WebMessageResult:
        normalized_email = normalize_email(email)
        if identity_mode == "external_id":
            identity_provider = "web_external_id"
            identity_value = (external_user_id or "").strip()
            if not identity_value or len(identity_value) > 255:
                raise ValueError("external_user_id is required")
        elif identity_mode == "email":
            identity_provider = "web_email"
            identity_value = normalized_email
        else:
            raise ValueError("unsupported Web identity mode")
        binding_uuid = canonical_remnawave_uuid(remnawave_user_uuid)
        if content is None and media is None:
            raise ValueError("text or photo is required")

        await self.ensure_web_identity_mode(identity_mode)

        replay = await self._load_replay(command)
        if replay is not None:
            return replay

        for attempt in range(3):
            async with self.database.session() as session:
                replay_payload = await load_api_replay_response(session, command)
                if replay_payload is not None:
                    break
                identity = await session.scalar(
                    select(UserIdentity).where(
                        UserIdentity.provider == identity_provider,
                        UserIdentity.external_id == identity_value,
                    )
                )
                user: User
                if identity is None:
                    user = User(
                        display_name=display_name.strip() if display_name else None,
                        username=None,
                        email=normalized_email,
                    )
                    user.identities.append(
                        UserIdentity(provider=identity_provider, external_id=identity_value)
                    )
                    session.add(user)
                    try:
                        await session.flush()
                    except IntegrityError:
                        await session.rollback()
                        if attempt < 2:
                            continue
                        raise
                else:
                    existing_user = await session.get(User, identity.user_id)
                    if existing_user is None:
                        raise RuntimeError("Web identity references a missing user")
                    user = existing_user
                    user.display_name = display_name.strip() if display_name else None
                    user.email = normalized_email

                ticket = await session.scalar(
                    select(Ticket)
                    .where(
                        Ticket.user_id == user.id,
                        Ticket.channel == TicketChannel.WEB,
                    )
                    .with_for_update()
                )
                created = ticket is None
                reopened = False
                suppressed = False
                now = utcnow()
                if ticket is None:
                    ticket = Ticket(
                        user_id=user.id,
                        channel=TicketChannel.WEB,
                        status=TicketStatus.PROVISIONING,
                        remnawave_user_uuid=binding_uuid,
                        last_activity_at=now,
                    )
                    session.add(ticket)
                    await session.flush()
                else:
                    if TicketChannel(ticket.channel) is not TicketChannel.WEB:
                        raise RuntimeError("Web identity is bound to a non-Web ticket")
                    suppressed = await self._is_ticket_blocked_in_session(session, ticket.id)
                    if binding_uuid is not None:
                        ticket.remnawave_user_uuid = binding_uuid
                    if not suppressed:
                        if TicketStatus(ticket.status) is TicketStatus.CLOSED:
                            ticket.status = (
                                TicketStatus.OPEN
                                if ticket.topic_id is not None
                                else TicketStatus.PROVISIONING
                            )
                            ticket.closed_at = None
                            reopened = True
                        ticket.last_activity_at = now

                message_id = str(uuid.uuid4())
                metadata = media.message_metadata() if media is not None else None
                message = TicketMessage(
                    id=message_id,
                    ticket_id=ticket.id,
                    direction=Direction.USER_TO_OPERATOR,
                    channel=TicketChannel.WEB.value,
                    content=content,
                    media=metadata,
                    suppressed=suppressed,
                )
                session.add(message)
                if media is not None:
                    session.add(
                        MediaAsset(
                            id=media.id,
                            ticket_id=ticket.id,
                            message_id=message_id,
                            storage_path=media.storage_path,
                            mime_type=media.mime_type,
                            size_bytes=media.size_bytes,
                            sha256=media.sha256,
                            original_filename=media.original_filename,
                        )
                    )
                if not suppressed:
                    delivery_payload: dict[str, object] = {
                        "kind": "send_photo" if media is not None else "send_text",
                        "target_chat_id": target_chat_id,
                        "target_thread_id": ticket.topic_id,
                        **({"text": content} if content is not None else {}),
                        **({"storage_path": media.storage_path} if media is not None else {}),
                    }
                    if reopened:
                        delivery_payload["prepare_reopened_context"] = True
                    session.add(
                        DeliveryOutbox(
                            ticket_id=ticket.id,
                            direction=Direction.USER_TO_OPERATOR,
                            idempotency_key=f"web-delivery:{command.storage_key}",
                            payload=delivery_payload,
                            status=(
                                DeliveryStatus.PENDING
                                if ticket.topic_id is not None
                                else DeliveryStatus.WAITING_TOPIC
                            ),
                        )
                    )
                    lifecycle_type = "created" if created else "reopened" if reopened else None
                    if lifecycle_type is not None:
                        session.add(
                            TicketLifecycleEvent(
                                ticket_id=ticket.id,
                                event_type=lifecycle_type,
                                channel=TicketChannel.WEB,
                                close_cycle=ticket.close_cycle,
                                created_at=now,
                            )
                        )
                        await enqueue_topic_reconciliation(
                            session, ticket_id=ticket.id, desired_status=TicketStatus.OPEN.value
                        )
                response = {
                    "ticket_id": ticket.id,
                    "message_id": message_id,
                    "created": created,
                    "reopened": reopened,
                }
                session.add(
                    OperatorAction(
                        ticket_id=ticket.id,
                        operator_telegram_id=0,
                        action="web_create_message",
                        idempotency_key=command.storage_key,
                        payload=api_action_payload(
                            {
                                "email": normalized_email,
                                "content": content,
                                "identity_provider": identity_provider,
                                "suppressed": suppressed,
                            },
                            command=command,
                            changed=True,
                            response=response,
                        ),
                        result="completed",
                        trace_id=get_trace_id(),
                    )
                )
                try:
                    await session.flush()
                    view = await self._ticket_view(session, ticket)
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    if attempt < 2:
                        continue
                    raise
                logger.info(
                    "Suppressed blocked Web support message"
                    if suppressed
                    else "Accepted Web support message",
                    extra={
                        "event": "web_message_suppressed" if suppressed else "web_message_accepted",
                        "ticket_id": ticket.id,
                        "email": normalized_email,
                        "message_content": content,
                        "created": created,
                        "reopened": reopened,
                        "suppressed": suppressed,
                    },
                )
                return WebMessageResult(
                    True,
                    created,
                    reopened,
                    view,
                    message_id,
                    _message_item(message),
                )
        replay = await self._load_replay(command)
        if replay is None:
            raise RuntimeError("concurrent Web message did not produce a durable result")
        return replay

    async def get_web_ticket(self, ticket_id: str) -> TicketView:
        async with self.database.session() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None or TicketChannel(ticket.channel) is not TicketChannel.WEB:
                raise TicketNotFoundError(ticket_id)
            return await self._ticket_view(session, ticket)

    async def list_messages(
        self, ticket_id: str, *, after: str | None, limit: int
    ) -> WebMessagePage:
        await self.get_web_ticket(ticket_id)
        statement = (
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at, TicketMessage.id)
            .limit(limit)
        )
        if after is not None:
            created_at, message_id = decode_cursor(after)
            statement = statement.where(
                or_(
                    TicketMessage.created_at > created_at,
                    and_(
                        TicketMessage.created_at == created_at,
                        TicketMessage.id > message_id,
                    ),
                )
            )
        async with self.database.session() as session:
            messages = list((await session.scalars(statement)).all())
        items = [_message_item(message) for message in messages]
        return WebMessagePage(items, items[-1].cursor if items else after)

    async def get_media(self, media_id: str) -> MediaAsset:
        async with self.database.session() as session:
            media = await session.get(MediaAsset, media_id)
            if media is None:
                raise TicketNotFoundError(media_id)
            ticket = await session.get(Ticket, media.ticket_id)
            if ticket is None or TicketChannel(ticket.channel) is not TicketChannel.WEB:
                raise TicketNotFoundError(media_id)
            return media

    @retry_sqlite_locks
    async def submit_rating(
        self,
        *,
        ticket_id: str,
        score: int,
        target_chat_id: int,
        command: ApiIdempotencyCommand,
    ) -> bool:
        if score not in range(1, 6):
            raise ValueError("score must be between 1 and 5")
        async with self.database.session() as session:
            replay = await load_api_replay_response(session, command)
            if replay is not None:
                return bool(replay["changed"])
            ticket = await session.scalar(
                select(Ticket).where(Ticket.id == ticket_id).with_for_update()
            )
            if (
                ticket is None
                or TicketChannel(ticket.channel) is not TicketChannel.WEB
                or TicketStatus(ticket.status) is not TicketStatus.CLOSED
            ):
                raise TicketNotFoundError(ticket_id)
            duplicate = await session.scalar(
                select(TicketMessage.id).where(
                    TicketMessage.ticket_id == ticket_id,
                    TicketMessage.channel == "rating",
                    TicketMessage.rating_cycle == ticket.close_cycle,
                )
            )
            suppressed = await self._is_ticket_blocked_in_session(session, ticket.id)
            changed = duplicate is None
            if changed:
                session.add(
                    TicketMessage(
                        ticket_id=ticket.id,
                        direction=Direction.USER_TO_OPERATOR,
                        channel="rating",
                        content=f"{score}/5",
                        media={"rating": score, "source": "web"},
                        rating_cycle=ticket.close_cycle,
                        suppressed=suppressed,
                    )
                )
                if not suppressed:
                    session.add(
                        TicketLifecycleEvent(
                            ticket_id=ticket.id,
                            event_type="rated",
                            channel=TicketChannel.WEB,
                            close_cycle=ticket.close_cycle,
                        )
                    )
                    ticket_link = ticket_topic_link(target_chat_id, ticket.topic_id)
                    ticket_link_suffix = f"\n\n{ticket_link}" if ticket_link else ""
                    session.add(
                        DeliveryOutbox(
                            ticket_id=ticket.id,
                            direction=Direction.USER_TO_OPERATOR,
                            idempotency_key=f"web-rating-delivery:{command.storage_key}",
                            payload={
                                "kind": "send_text",
                                "target_chat_id": target_chat_id,
                                "target_system_topic": "ratings",
                                "text": (
                                    f"⭐ <b>Оценка поддержки</b>\n\n"
                                    f"Web-клиент: <b>{score}/5</b>"
                                    f"{ticket_link_suffix}"
                                ),
                                "parse_mode": "HTML",
                            },
                            status=DeliveryStatus.PENDING,
                        )
                    )
            session.add(
                OperatorAction(
                    ticket_id=ticket.id,
                    operator_telegram_id=0,
                    action="web_rate_ticket",
                    idempotency_key=command.storage_key,
                    payload=api_action_payload({}, command=command, changed=changed),
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = await load_api_replay_response(session, command)
                if replay is None:
                    raise
                return bool(replay["changed"])
            return changed
