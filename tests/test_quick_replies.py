from __future__ import annotations

from pathlib import Path

import pytest

from suppsystem.database import Database
from suppsystem.quick_replies import (
    QuickReplyGroupNameConflictError,
    QuickReplyGroupNotFoundError,
    QuickReplyService,
    QuickReplyTitleConflictError,
)


async def _create_group(
    service: QuickReplyService,
    *,
    name: str,
    source_message_id: int,
):
    return await service.create_group(
        name=name,
        operator_telegram_id=7,
        operator_display_name="Operator",
        operator_username="operator",
        source_chat_id=-100123,
        source_message_id=source_message_id,
    )


async def test_quick_reply_groups_are_shared_normalized_and_idempotent(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-reply-groups.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        created = await _create_group(
            service,
            name="  НЕ   РАБОТАЕТ AI  ",
            source_message_id=101,
        )

        assert created.created is True
        assert created.group.name == "НЕ РАБОТАЕТ AI"

        duplicate = await _create_group(
            service,
            name="Другое название",
            source_message_id=101,
        )
        assert duplicate.created is False
        assert duplicate.group.id == created.group.id

        with pytest.raises(QuickReplyGroupNameConflictError):
            await _create_group(
                service,
                name=" не работает ai ",
                source_message_id=102,
            )

        second = await _create_group(
            service,
            name="Оплата",
            source_message_id=103,
        )
        groups, total = await service.list_groups(offset=1, limit=1)
        assert total == 2
        assert [group.id for group in groups] == [second.group.id]

        fetched = await service.get_active_group_by_name("  не РАБОТАЕТ   ai ")
        assert fetched is not None
        assert fetched.id == created.group.id

        assert await service.mark_group_published(created.group.id, 901) is True
        assert await service.mark_group_published(created.group.id, 902) is False
        published = await service.get_active_group(created.group.id)
        assert published is not None
        assert published.published_message_id == 901
    finally:
        await database.dispose()


async def test_quick_replies_are_scoped_to_groups_and_published_once(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-replies.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        ai_group = (await _create_group(service, name="AI", source_message_id=201)).group
        billing_group = (await _create_group(service, name="Оплата", source_message_id=202)).group

        first = await service.create(
            group_id=ai_group.id,
            title="  AI   не отвечает  ",
            text="  Перезапустите приложение.  ",
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=301,
        )
        assert first.created is True
        assert first.reply.group_id == ai_group.id
        assert first.reply.title == "AI не отвечает"
        assert first.reply.text == "Перезапустите приложение."

        duplicate = await service.create(
            group_id=billing_group.id,
            title="Другое название",
            text="Другой текст",
            operator_telegram_id=8,
            operator_display_name=None,
            operator_username=None,
            source_chat_id=-100123,
            source_message_id=301,
        )
        assert duplicate.created is False
        assert duplicate.reply.id == first.reply.id

        with pytest.raises(QuickReplyTitleConflictError):
            await service.create(
                group_id=ai_group.id,
                title=" ai НЕ отвечает ",
                text="Конфликтующий текст",
                operator_telegram_id=8,
                operator_display_name=None,
                operator_username=None,
                source_chat_id=-100123,
                source_message_id=302,
            )

        same_title_other_group = await service.create(
            group_id=billing_group.id,
            title="AI не отвечает",
            text="Ответ для оплаты",
            operator_telegram_id=8,
            operator_display_name=None,
            operator_username=None,
            source_chat_id=-100123,
            source_message_id=303,
        )
        assert same_title_other_group.created is True

        with pytest.raises(QuickReplyGroupNotFoundError):
            await service.create(
                group_id=9999,
                title="Не сохранится",
                text="Нет группы",
                operator_telegram_id=7,
                operator_display_name=None,
                operator_username=None,
                source_chat_id=-100123,
                source_message_id=304,
            )

        ai_replies, ai_total = await service.list_active(
            group_id=ai_group.id,
            offset=0,
            limit=10,
        )
        billing_replies, billing_total = await service.list_active(
            group_id=billing_group.id,
            offset=0,
            limit=10,
        )
        assert ai_total == 1
        assert [reply.id for reply in ai_replies] == [first.reply.id]
        assert billing_total == 1
        assert [reply.id for reply in billing_replies] == [same_title_other_group.reply.id]

        assert await service.mark_published(first.reply.id, 911) is True
        assert await service.mark_published(first.reply.id, 912) is False
        published = await service.get_active(first.reply.id)
        assert published is not None
        assert published.published_message_id == 911
    finally:
        await database.dispose()
