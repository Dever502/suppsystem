from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TicketStatus(enum.StrEnum):
    PROVISIONING = "provisioning"
    OPEN = "open"
    CLOSED = "closed"


class DeliveryStatus(enum.StrEnum):
    WAITING_TOPIC = "waiting_topic"
    PENDING = "pending"
    PROCESSING = "processing"
    CANCELLED = "cancelled"
    DELIVERED = "delivered"
    FAILED = "failed"


class NotificationStatus(enum.StrEnum):
    AWAITING_PAYLOAD = "awaiting_payload"
    CANCELLED = "cancelled"
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"


class WorkStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"


class Direction(enum.StrEnum):
    USER_TO_OPERATOR = "user_to_operator"
    OPERATOR_TO_USER = "operator_to_user"


class TicketChannel(enum.StrEnum):
    TELEGRAM = "telegram"
    WEB = "web"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    identities: Mapped[list[UserIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    tickets: Mapped[list[Ticket]] = relationship(back_populates="user")


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_identity_provider_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="identities")


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_status_updated", "status", "updated_at"),
        Index("ix_tickets_status_last_activity", "status", "last_activity_at"),
        UniqueConstraint("user_id", "channel", name="uq_ticket_user_channel"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[TicketChannel] = mapped_column(
        String(32), default=TicketChannel.TELEGRAM, nullable=False
    )
    remnawave_user_uuid: Mapped[str | None] = mapped_column(String(36))
    topic_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    status: Mapped[TicketStatus] = mapped_column(
        String(32), default=TicketStatus.PROVISIONING, nullable=False
    )
    topic_provisioning_token: Mapped[str | None] = mapped_column(String(36), unique=True)
    topic_provisioning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_cycle: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="tickets")
    messages: Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    __table_args__ = (
        UniqueConstraint(
            "direction", "source_chat_id", "source_message_id", name="uq_ticket_message_source"
        ),
        UniqueConstraint(
            "ticket_id",
            "channel",
            "rating_cycle",
            name="uq_ticket_message_rating_cycle",
        ),
        Index("ix_ticket_messages_ticket_created", "ticket_id", "created_at"),
        Index(
            "ix_ticket_messages_direction_channel_created",
            "direction",
            "channel",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[Direction] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), default="telegram", nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    media: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    suppressed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    rating_cycle: Mapped[int | None] = mapped_column(Integer)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")


class DeliveryOutbox(Base):
    __tablename__ = "delivery_outbox"
    __table_args__ = (
        Index("ix_delivery_outbox_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_delivery_outbox_stale", "status", "claimed_at"),
        Index(
            "ix_delivery_outbox_ticket_direction_status",
            "ticket_id",
            "direction",
            "status",
        ),
        Index(
            "ix_delivery_outbox_ticket_status_created",
            "ticket_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    direction: Mapped[Direction] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        String(32), default=DeliveryStatus.PENDING, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(36))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("ix_notification_outbox_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_notification_outbox_stale", "status", "claimed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    operator_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("operator_actions.id", ondelete="SET NULL"),
        unique=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_identity_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_identity_value: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        String(32), default=NotificationStatus.PENDING, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(36))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboundUpdate(Base):
    __tablename__ = "inbound_updates"
    __table_args__ = (
        Index("ix_inbound_updates_claim", "status", "next_attempt_at", "telegram_update_id"),
        Index("ix_inbound_updates_stale", "status", "claimed_at"),
    )

    telegram_update_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[WorkStatus] = mapped_column(
        String(32), default=WorkStatus.PENDING, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(36))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReconciliationOutbox(Base):
    __tablename__ = "reconciliation_outbox"
    __table_args__ = (
        Index("ix_reconciliation_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_reconciliation_stale", "status", "claimed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    operator_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("operator_actions.id", ondelete="CASCADE"), unique=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[WorkStatus] = mapped_column(
        String(32), default=WorkStatus.PENDING, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(36))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperatorAction(Base):
    __tablename__ = "operator_actions"
    __table_args__ = (
        Index(
            "ix_operator_actions_ticket_action_result",
            "ticket_id",
            "action",
            "result",
        ),
        Index("ix_operator_actions_result_created", "result", "created_at"),
        Index(
            "uq_operator_actions_unresolved_ticket",
            "ticket_id",
            unique=True,
            sqlite_where=text("result IN ('started', 'unknown') AND action LIKE 'remnawave_%'"),
            postgresql_where=text("result IN ('started', 'unknown') AND action LIKE 'remnawave_%'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id", ondelete="SET NULL"))
    operator_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("result")
    def track_result_timestamp(self, key: str, value: str | None) -> str | None:
        now = utcnow()
        self.updated_at = now
        if value not in {None, "started", "unknown"} and self.completed_at is None:
            self.completed_at = now
        return value


class BlocklistEntry(Base):
    __tablename__ = "blocklist"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    blocked_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SupportBlock(Base):
    __tablename__ = "support_blocks"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True
    )
    blocked_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="telegram", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
