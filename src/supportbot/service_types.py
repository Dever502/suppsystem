from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from supportbot.models import Direction, TicketStatus


class TicketNotFoundError(Exception):
    pass


class TopicAlreadyBoundError(Exception):
    pass


class TopicProvisioningConflictError(Exception):
    pass


@dataclass(frozen=True)
class TicketView:
    id: str
    user_id: int
    telegram_user_id: int
    display_name: str | None
    username: str | None
    topic_id: int | None
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None
    close_cycle: int = 0
    reopened: bool = False


@dataclass(frozen=True)
class InternalNoteView:
    content: str
    operator_telegram_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class DeliveryJob:
    id: str
    ticket_id: str
    payload: dict[str, object]
    attempt_count: int
    claim_token: str
    direction: Direction = Direction.USER_TO_OPERATOR


@dataclass(frozen=True)
class NotificationJob:
    id: str
    ticket_id: str
    event_type: str
    destination: str
    recipient_identity_provider: str
    recipient_identity_value: str
    payload: dict[str, object]
    created_at: datetime
    attempt_count: int
    claim_token: str
