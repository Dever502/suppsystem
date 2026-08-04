from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from suppsystem.database import Database, retry_sqlite_locks
from suppsystem.models import QuickResponse
from suppsystem.web_models import SystemSetting

QUICK_RESPONSE_TEXT_MAX_LENGTH = 4096
QUICK_RESPONSE_MAX_TAGS = 4
QUICK_RESPONSE_VALID = "valid"
QUICK_RESPONSE_PENDING_DELETION = "pending_deletion"


@dataclass(frozen=True)
class QuickResponseView:
    id: int
    text: str
    tags: tuple[str, ...]
    created_by_telegram_id: int
    created_by_display_name: str | None
    created_by_username: str | None
    source_chat_id: int
    source_message_id: int
    published_message_id: int | None
    state: str
    invalid_until: datetime | None
    status_message_id: int | None
    created_at: datetime
    updated_at: datetime


def utf16_code_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _view(response: QuickResponse) -> QuickResponseView:
    return QuickResponseView(
        id=response.id,
        text=response.text,
        tags=tuple(response.tags),
        created_by_telegram_id=response.created_by_telegram_id,
        created_by_display_name=response.created_by_display_name,
        created_by_username=response.created_by_username,
        source_chat_id=response.source_chat_id,
        source_message_id=response.source_message_id,
        published_message_id=response.published_message_id,
        state=response.state,
        invalid_until=_as_utc(response.invalid_until),
        status_message_id=response.status_message_id,
        created_at=response.created_at,
        updated_at=response.updated_at,
    )


class QuickReplyService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get_by_source(
        self,
        *,
        source_chat_id: int,
        source_message_id: int,
    ) -> QuickResponseView | None:
        async with self.database.session() as session:
            response = await session.scalar(
                select(QuickResponse).where(
                    QuickResponse.source_chat_id == source_chat_id,
                    QuickResponse.source_message_id == source_message_id,
                )
            )
        return _view(response) if response is not None else None

    @retry_sqlite_locks
    async def save_valid(
        self,
        *,
        text: str,
        tags: list[str],
        operator_telegram_id: int,
        operator_display_name: str | None,
        operator_username: str | None,
        source_chat_id: int,
        source_message_id: int,
    ) -> QuickResponseView:
        return await self._upsert(
            text=text,
            tags=tags,
            operator_telegram_id=operator_telegram_id,
            operator_display_name=operator_display_name,
            operator_username=operator_username,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            state=QUICK_RESPONSE_VALID,
            invalid_until=None,
        )

    @retry_sqlite_locks
    async def save_pending_deletion(
        self,
        *,
        text: str,
        tags: list[str],
        operator_telegram_id: int,
        operator_display_name: str | None,
        operator_username: str | None,
        source_chat_id: int,
        source_message_id: int,
        invalid_until: datetime,
    ) -> QuickResponseView:
        return await self._upsert(
            text=text,
            tags=tags,
            operator_telegram_id=operator_telegram_id,
            operator_display_name=operator_display_name,
            operator_username=operator_username,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            state=QUICK_RESPONSE_PENDING_DELETION,
            invalid_until=invalid_until,
        )

    async def _upsert(
        self,
        *,
        text: str,
        tags: list[str],
        operator_telegram_id: int,
        operator_display_name: str | None,
        operator_username: str | None,
        source_chat_id: int,
        source_message_id: int,
        state: str,
        invalid_until: datetime | None,
    ) -> QuickResponseView:
        if not text.strip() or utf16_code_units(text) > QUICK_RESPONSE_TEXT_MAX_LENGTH:
            raise ValueError("invalid quick response text")
        if state not in {QUICK_RESPONSE_VALID, QUICK_RESPONSE_PENDING_DELETION}:
            raise ValueError("invalid quick response state")

        for attempt in range(2):
            async with self.database.session() as session:
                response = await session.scalar(
                    select(QuickResponse).where(
                        QuickResponse.source_chat_id == source_chat_id,
                        QuickResponse.source_message_id == source_message_id,
                    )
                )
                if response is None:
                    response = QuickResponse(
                        text=text,
                        tags=list(tags),
                        created_by_telegram_id=operator_telegram_id,
                        created_by_display_name=operator_display_name,
                        created_by_username=operator_username,
                        source_chat_id=source_chat_id,
                        source_message_id=source_message_id,
                        published_message_id=source_message_id,
                        state=state,
                        invalid_until=invalid_until,
                    )
                    session.add(response)
                else:
                    response.text = text
                    response.tags = list(tags)
                    response.created_by_telegram_id = operator_telegram_id
                    response.created_by_display_name = operator_display_name
                    response.created_by_username = operator_username
                    response.published_message_id = source_message_id
                    response.state = state
                    response.invalid_until = invalid_until
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    if attempt == 0:
                        continue
                    raise
                return _view(response)
        raise RuntimeError("unreachable quick response upsert state")

    @retry_sqlite_locks
    async def attach_status_message(
        self,
        response_id: int,
        status_message_id: int,
        *,
        expected_state: str,
    ) -> bool:
        async with self.database.session() as session:
            response = await session.get(QuickResponse, response_id)
            if response is None or response.state != expected_state:
                return False
            if response.status_message_id is not None:
                return response.status_message_id == status_message_id
            response.status_message_id = status_message_id
            await session.commit()
            return True

    @retry_sqlite_locks
    async def clear_status_message(self, response_id: int, status_message_id: int) -> bool:
        async with self.database.session() as session:
            response = await session.get(QuickResponse, response_id)
            if response is None or response.status_message_id != status_message_id:
                return False
            response.status_message_id = None
            await session.commit()
            return True

    async def list_valid(self) -> list[QuickResponseView]:
        async with self.database.session() as session:
            responses = list(
                (
                    await session.scalars(
                        select(QuickResponse)
                        .where(QuickResponse.state == QUICK_RESPONSE_VALID)
                        .order_by(QuickResponse.id)
                    )
                ).all()
            )
        return [_view(response) for response in responses]

    async def list_pending_deletion(self) -> list[QuickResponseView]:
        async with self.database.session() as session:
            responses = list(
                (
                    await session.scalars(
                        select(QuickResponse)
                        .where(QuickResponse.state == QUICK_RESPONSE_PENDING_DELETION)
                        .order_by(QuickResponse.invalid_until, QuickResponse.id)
                    )
                ).all()
            )
        return [_view(response) for response in responses]

    @retry_sqlite_locks
    async def delete_if_still_pending(
        self,
        response_id: int,
        *,
        invalid_until: datetime,
    ) -> QuickResponseView | None:
        async with self.database.session() as session:
            response = await session.get(QuickResponse, response_id)
            if (
                response is None
                or response.state != QUICK_RESPONSE_PENDING_DELETION
                or _as_utc(response.invalid_until) != _as_utc(invalid_until)
            ):
                return None
            view = _view(response)
            await session.delete(response)
            await session.commit()
            return view

    @retry_sqlite_locks
    async def discard_all_pending(self) -> list[QuickResponseView]:
        async with self.database.session() as session:
            responses = list(
                (
                    await session.scalars(
                        select(QuickResponse).where(
                            QuickResponse.state == QUICK_RESPONSE_PENDING_DELETION
                        )
                    )
                ).all()
            )
            views = [_view(response) for response in responses]
            if responses:
                await session.execute(
                    delete(QuickResponse).where(
                        QuickResponse.state == QUICK_RESPONSE_PENDING_DELETION
                    )
                )
                await session.commit()
            return views

    @retry_sqlite_locks
    async def mark_published(self, response_id: int, message_id: int) -> None:
        async with self.database.session() as session:
            response = await session.get(QuickResponse, response_id)
            if response is None:
                return
            response.published_message_id = message_id
            await session.commit()

    @staticmethod
    def _instruction_setting_key(support_group_id: int) -> str:
        return f"telegram_quick_response_instruction:{support_group_id}"

    @staticmethod
    def _instruction_topic_setting_key(support_group_id: int) -> str:
        return f"telegram_quick_response_instruction_topic:{support_group_id}"

    @staticmethod
    def _legacy_menu_setting_key(support_group_id: int) -> str:
        return f"telegram_quick_reply_menu:{support_group_id}"

    async def instruction_message_id(self, support_group_id: int) -> int | None:
        async with self.database.session() as session:
            setting = await session.get(
                SystemSetting,
                self._instruction_setting_key(support_group_id),
            )
        if setting is None:
            return None
        try:
            message_id = int(setting.value)
        except ValueError:
            return None
        return message_id if message_id > 0 else None

    @retry_sqlite_locks
    async def save_instruction_message_id(
        self,
        support_group_id: int,
        message_id: int,
        topic_id: int,
    ) -> None:
        async with self.database.session() as session:
            key = self._instruction_setting_key(support_group_id)
            setting = await session.get(SystemSetting, key)
            if setting is None:
                session.add(SystemSetting(key=key, value=str(message_id)))
            else:
                setting.value = str(message_id)
            topic_key = self._instruction_topic_setting_key(support_group_id)
            topic_setting = await session.get(SystemSetting, topic_key)
            if topic_setting is None:
                session.add(SystemSetting(key=topic_key, value=str(topic_id)))
            else:
                topic_setting.value = str(topic_id)
            await session.commit()

    async def instruction_topic_id(self, support_group_id: int) -> int | None:
        async with self.database.session() as session:
            setting = await session.get(
                SystemSetting,
                self._instruction_topic_setting_key(support_group_id),
            )
        if setting is None:
            return None
        try:
            topic_id = int(setting.value)
        except ValueError:
            return None
        return topic_id if topic_id > 0 else None

    async def legacy_message_ids(self, support_group_id: int) -> list[int]:
        prefix = "telegram_quick_reply_legacy:"
        async with self.database.session() as session:
            settings = list(
                (
                    await session.scalars(
                        select(SystemSetting).where(
                            (SystemSetting.key == self._legacy_menu_setting_key(support_group_id))
                            | SystemSetting.key.startswith(prefix)
                        )
                    )
                ).all()
            )
        message_ids: set[int] = set()
        for setting in settings:
            try:
                message_id = int(setting.value)
            except ValueError:
                continue
            if message_id > 0:
                message_ids.add(message_id)
        return sorted(message_ids)

    @retry_sqlite_locks
    async def finish_legacy_cleanup(self, support_group_id: int) -> None:
        prefix = "telegram_quick_reply_legacy:"
        async with self.database.session() as session:
            await session.execute(
                delete(SystemSetting).where(
                    (SystemSetting.key == self._legacy_menu_setting_key(support_group_id))
                    | SystemSetting.key.startswith(prefix)
                )
            )
            await session.commit()
