from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Header, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator

from suppsystem.models import Direction, TicketStatus
from suppsystem.service_types import TicketView

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
TICKET_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
IdempotencyKey = Annotated[
    str,
    Header(
        alias="X-Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
]
TicketId = Annotated[str, Path(min_length=36, max_length=36, pattern=TICKET_ID_PATTERN)]


class TicketResponse(BaseModel):
    id: str
    telegram_user_id: int
    display_name: str | None
    username: str | None
    topic_id: int | None
    status: TicketStatus
    last_activity_at: datetime


class MessageResponse(BaseModel):
    id: str
    direction: Direction
    channel: str
    content: str | None
    media: dict[str, object] | None
    source_chat_id: int | None
    source_message_id: int | None
    created_at: datetime


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SendMessageRequest(StrictRequest):
    text: str = Field(min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class CloseTicketRequest(StrictRequest):
    notify_user: bool = False


class MutationResponse(BaseModel):
    changed: bool


def ticket_response(ticket: TicketView) -> TicketResponse:
    return TicketResponse(
        id=ticket.id,
        telegram_user_id=ticket.telegram_user_id,
        display_name=ticket.display_name,
        username=ticket.username,
        topic_id=ticket.topic_id,
        status=ticket.status,
        last_activity_at=ticket.last_activity_at,
    )
