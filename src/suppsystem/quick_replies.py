from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from suppsystem.database import Database, retry_sqlite_locks
from suppsystem.models import QuickReply

QUICK_REPLY_TITLE_MAX_LENGTH = 64
QUICK_REPLY_TEXT_MAX_LENGTH = 3500


class QuickReplyTitleConflictError(Exception):
    pass


@dataclass(frozen=True)
class QuickReplyView:
    id: int
    title: str
    text: str
    created_by_telegram_id: int
    created_by_display_name: str | None
    created_by_username: str | None
    published_message_id: int | None
    active: bool
    created_at: datetime


@dataclass(frozen=True)
class QuickReplyCreateResult:
    reply: QuickReplyView
    created: bool


def utf16_code_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def normalize_quick_reply_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def _view(reply: QuickReply) -> QuickReplyView:
    return QuickReplyView(
        id=reply.id,
        title=reply.title,
        text=reply.text,
        created_by_telegram_id=reply.created_by_telegram_id,
        created_by_display_name=reply.created_by_display_name,
        created_by_username=reply.created_by_username,
        published_message_id=reply.published_message_id,
        active=reply.active,
        created_at=reply.created_at,
    )


class QuickReplyService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @retry_sqlite_locks
    async def create(
        self,
        *,
        title: str,
        text: str,
        operator_telegram_id: int,
        operator_display_name: str | None,
        operator_username: str | None,
        source_chat_id: int,
        source_message_id: int,
    ) -> QuickReplyCreateResult:
        clean_title = " ".join(title.split())
        clean_text = text.strip()
        if not clean_title or utf16_code_units(clean_title) > QUICK_REPLY_TITLE_MAX_LENGTH:
            raise ValueError("invalid quick reply title")
        if not clean_text or utf16_code_units(clean_text) > QUICK_REPLY_TEXT_MAX_LENGTH:
            raise ValueError("invalid quick reply text")
        normalized_title = normalize_quick_reply_title(clean_title)

        async with self.database.session() as session:
            existing = await session.scalar(
                select(QuickReply).where(
                    QuickReply.source_chat_id == source_chat_id,
                    QuickReply.source_message_id == source_message_id,
                )
            )
            if existing is not None:
                return QuickReplyCreateResult(reply=_view(existing), created=False)
            if await session.scalar(
                select(QuickReply.id).where(QuickReply.normalized_title == normalized_title)
            ):
                raise QuickReplyTitleConflictError(clean_title)

            reply = QuickReply(
                title=clean_title,
                normalized_title=normalized_title,
                text=clean_text,
                created_by_telegram_id=operator_telegram_id,
                created_by_display_name=operator_display_name,
                created_by_username=operator_username,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
            )
            session.add(reply)
            try:
                await session.flush()
                result = QuickReplyCreateResult(reply=_view(reply), created=True)
                await session.commit()
                return result
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(QuickReply).where(
                        QuickReply.source_chat_id == source_chat_id,
                        QuickReply.source_message_id == source_message_id,
                    )
                )
                if existing is not None:
                    return QuickReplyCreateResult(reply=_view(existing), created=False)
                if await session.scalar(
                    select(QuickReply.id).where(QuickReply.normalized_title == normalized_title)
                ):
                    raise QuickReplyTitleConflictError(clean_title) from None
                raise

    async def list_active(self, *, offset: int, limit: int) -> tuple[list[QuickReplyView], int]:
        async with self.database.session() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(QuickReply).where(QuickReply.active.is_(True))
                )
                or 0
            )
            replies = list(
                (
                    await session.scalars(
                        select(QuickReply)
                        .where(QuickReply.active.is_(True))
                        .order_by(QuickReply.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
        return [_view(reply) for reply in replies], total

    async def get_active(self, reply_id: int) -> QuickReplyView | None:
        async with self.database.session() as session:
            reply = await session.scalar(
                select(QuickReply).where(
                    QuickReply.id == reply_id,
                    QuickReply.active.is_(True),
                )
            )
        return _view(reply) if reply is not None else None

    @retry_sqlite_locks
    async def mark_published(self, reply_id: int, message_id: int) -> bool:
        async with self.database.session() as session:
            result = await session.execute(
                update(QuickReply)
                .where(
                    QuickReply.id == reply_id,
                    QuickReply.published_message_id.is_(None),
                )
                .values(published_message_id=message_id)
            )
            changed = cast(CursorResult[object], result).rowcount == 1
            await session.commit()
            return changed
