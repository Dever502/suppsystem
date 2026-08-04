from __future__ import annotations

from pathlib import Path

import pytest

from suppsystem.database import Database
from suppsystem.quick_replies import (
    QuickReplyService,
    QuickReplyTitleConflictError,
)


async def test_quick_reply_create_is_idempotent_and_titles_are_unique(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-replies.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        created = await service.create(
            title="  AI   не отвечает  ",
            text="  Перезапустите приложение.  ",
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=101,
        )

        assert created.created is True
        assert created.reply.title == "AI не отвечает"
        assert created.reply.text == "Перезапустите приложение."

        duplicate = await service.create(
            title="Другое название",
            text="Другой текст",
            operator_telegram_id=8,
            operator_display_name=None,
            operator_username=None,
            source_chat_id=-100123,
            source_message_id=101,
        )
        assert duplicate.created is False
        assert duplicate.reply.id == created.reply.id

        with pytest.raises(QuickReplyTitleConflictError):
            await service.create(
                title=" ai НЕ отвечает ",
                text="Конфликтующий текст",
                operator_telegram_id=8,
                operator_display_name=None,
                operator_username=None,
                source_chat_id=-100123,
                source_message_id=102,
            )
    finally:
        await database.dispose()


async def test_quick_reply_catalog_and_publication_state(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-reply-list.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        first = await service.create(
            title="Первый",
            text="Первый текст",
            operator_telegram_id=7,
            operator_display_name=None,
            operator_username=None,
            source_chat_id=-100123,
            source_message_id=201,
        )
        second = await service.create(
            title="Второй",
            text="Второй текст",
            operator_telegram_id=7,
            operator_display_name=None,
            operator_username=None,
            source_chat_id=-100123,
            source_message_id=202,
        )

        page, total = await service.list_active(offset=1, limit=1)
        assert total == 2
        assert [reply.id for reply in page] == [second.reply.id]
        fetched = await service.get_active(first.reply.id)
        assert fetched is not None
        assert (fetched.id, fetched.title, fetched.text) == (
            first.reply.id,
            first.reply.title,
            first.reply.text,
        )

        assert await service.mark_published(first.reply.id, 901) is True
        assert await service.mark_published(first.reply.id, 902) is False
        published = await service.get_active(first.reply.id)
        assert published is not None
        assert published.published_message_id == 901
    finally:
        await database.dispose()
