from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from suppsystem.database import Database
from suppsystem.quick_replies import (
    QUICK_RESPONSE_PENDING_DELETION,
    QUICK_RESPONSE_PUBLICATION_FORMAT_VERSION,
    QUICK_RESPONSE_VALID,
    QuickReplyService,
)
from suppsystem.web_models import SystemSetting


def _operator_fields() -> dict[str, object]:
    return {
        "operator_telegram_id": 7,
        "operator_display_name": "Operator",
        "operator_username": "operator",
        "source_chat_id": -100123,
    }


async def test_quick_response_is_created_and_updated_by_source_message(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-responses.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        created = await service.save_valid(
            text="Переустановите TikTok #TikTok",
            tags=["#TikTok"],
            source_message_id=301,
            **_operator_fields(),  # type: ignore[arg-type]
        )

        assert created.state == QUICK_RESPONSE_VALID
        assert created.tags == ("#TikTok",)
        assert created.published_message_id is None
        assert created.publication_format_version == 0

        await service.record_publication(created.id, 901)
        assert await service.complete_publication(created.id, 901) is True

        updated = await service.save_valid(
            text="Обновите TikTok #TikTok #Android",
            tags=["#TikTok", "#Android"],
            source_message_id=301,
            **_operator_fields(),  # type: ignore[arg-type]
        )

        assert updated.id == created.id
        assert updated.text == "Обновите TikTok #TikTok #Android"
        assert updated.tags == ("#TikTok", "#Android")
        assert updated.published_message_id == 901
        assert updated.publication_format_version == QUICK_RESPONSE_PUBLICATION_FORMAT_VERSION
        assert [item.id for item in await service.list_valid()] == [created.id]
    finally:
        await database.dispose()


async def test_pending_response_preserves_deadline_and_can_become_valid(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/pending-response.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        pending = await service.save_pending_deletion(
            text="Текст #1 #2 #3 #4 #5",
            tags=["#1", "#2", "#3", "#4", "#5"],
            source_message_id=302,
            invalid_until=deadline,
            **_operator_fields(),  # type: ignore[arg-type]
        )
        assert pending.state == QUICK_RESPONSE_PENDING_DELETION
        assert pending.invalid_until == deadline
        assert await service.attach_warning(pending.id, 900) is True

        corrected = await service.save_valid(
            text="Текст #1 #2",
            tags=["#1", "#2"],
            source_message_id=302,
            **_operator_fields(),  # type: ignore[arg-type]
        )
        assert corrected.id == pending.id
        assert corrected.state == QUICK_RESPONSE_VALID
        assert corrected.invalid_until is None
        assert corrected.warning_message_id == 900
        assert await service.clear_warning(corrected.id, 900) is True
        assert (
            await service.delete_if_still_pending(
                pending.id,
                invalid_until=deadline,
            )
            is None
        )
    finally:
        await database.dispose()


async def test_pending_response_is_deleted_only_for_matching_deadline(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/pending-delete.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        pending = await service.save_pending_deletion(
            text="Текст #1 #2 #3 #4 #5",
            tags=["#1", "#2", "#3", "#4", "#5"],
            source_message_id=303,
            invalid_until=deadline,
            **_operator_fields(),  # type: ignore[arg-type]
        )

        assert (
            await service.delete_if_still_pending(
                pending.id,
                invalid_until=deadline + timedelta(seconds=1),
            )
            is None
        )
        deleted = await service.delete_if_still_pending(
            pending.id,
            invalid_until=deadline,
        )
        assert deleted is not None
        assert (
            await service.get_by_source(
                source_chat_id=-100123,
                source_message_id=303,
            )
            is None
        )
    finally:
        await database.dispose()


async def test_instruction_and_legacy_cleanup_state_is_durable(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-response-settings.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        async with database.session() as session:
            session.add(
                SystemSetting(
                    key="telegram_quick_reply_menu:-100123",
                    value="800",
                )
            )
            session.add(
                SystemSetting(
                    key="telegram_quick_reply_legacy:0",
                    value="801",
                )
            )
            await session.commit()

        assert await service.legacy_message_ids(-100123) == [800, 801]
        await service.save_instruction_message_id(-100123, 900, 777)
        assert await service.instruction_message_id(-100123) == 900
        assert await service.instruction_topic_id(-100123) == 777

        await service.finish_legacy_cleanup(-100123)
        assert await service.legacy_message_ids(-100123) == []
        assert await service.instruction_message_id(-100123) == 900
        assert await service.instruction_topic_id(-100123) == 777
    finally:
        await database.dispose()
