from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from suppsystem.models import Direction, TicketStatus
from suppsystem.service_types import TicketView
from suppsystem.web_support_service import WebMessageItem, WebMessageResult


class StrictWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebMessageRequest(StrictWebRequest):
    external_user_id: str | None = Field(default=None, min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=255)
    remnawave_user_uuid: str | None = Field(default=None, min_length=36, max_length=36)
    text: str | None = Field(default=None, max_length=4096)

    @field_validator("email")
    @classmethod
    def validate_email_shape(cls, value: str) -> str:
        normalized = value.strip()
        if normalized.count("@") != 1 or "." not in normalized.rsplit("@", maxsplit=1)[1]:
            raise ValueError("email is invalid")
        return normalized

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text must not be blank")
        return value


class WebConversationResponse(BaseModel):
    id: str
    source: Literal["Web"] = "Web"
    email: str
    display_name: str | None
    status: TicketStatus
    close_cycle: int
    created_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None


class WebAcceptedMessageResponse(BaseModel):
    conversation: WebConversationResponse
    conversation_id: str
    message: WebMessageResponse
    message_id: str
    next_cursor: str
    changed: bool
    created: bool
    reopened: bool


class WebMessageResponse(BaseModel):
    id: str
    direction: Direction
    type: Literal["text", "photo"]
    text: str | None
    created_at: datetime
    cursor: str
    media_url: str | None = None
    media_mime_type: str | None = None


class WebMessagesResponse(BaseModel):
    items: list[WebMessageResponse]
    next_cursor: str | None


class WebBlockRequest(StrictWebRequest):
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class WebRatingRequest(StrictWebRequest):
    score: int = Field(ge=1, le=5)


class WebMutationResponse(BaseModel):
    changed: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else None


def conversation_response(ticket: TicketView) -> WebConversationResponse:
    if ticket.email is None:
        raise RuntimeError("Web ticket has no email")
    return WebConversationResponse(
        id=ticket.id,
        email=ticket.email,
        display_name=ticket.display_name,
        status=ticket.status,
        close_cycle=ticket.close_cycle,
        created_at=_utc(ticket.created_at),
        last_activity_at=_utc(ticket.last_activity_at),
        closed_at=_optional_utc(ticket.closed_at),
    )


def accepted_message_response(result: WebMessageResult) -> WebAcceptedMessageResponse:
    return WebAcceptedMessageResponse(
        conversation=conversation_response(result.ticket),
        conversation_id=result.ticket.id,
        message=message_response(result.message),
        message_id=result.message_id,
        next_cursor=result.message.cursor,
        changed=result.changed,
        created=result.created,
        reopened=result.reopened,
    )


def message_response(message: WebMessageItem) -> WebMessageResponse:
    return WebMessageResponse(
        id=message.id,
        direction=message.direction,
        type="photo" if message.media_id is not None else "text",
        text=message.content,
        created_at=_utc(message.created_at),
        cursor=message.cursor,
        media_url=(f"/api/v1/web/media/{message.media_id}" if message.media_id else None),
        media_mime_type=message.media_mime_type,
    )
