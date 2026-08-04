from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Message, MessageEntity
from pydantic import SecretStr

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.quick_replies import (
    QUICK_RESPONSE_PENDING_DELETION,
    QUICK_RESPONSE_VALID,
    QuickReplyService,
)
from suppsystem.telegram_quick_replies import (
    QUICK_RESPONSE_INSTRUCTION_TEXT,
    QUICK_RESPONSE_SAVED_TEXT,
    QUICK_RESPONSE_WARNING_TEXT,
    QuickResponseTopicRefreshWorker,
    TelegramQuickReplyHandlers,
)


class FakeLimiter:
    def __init__(self) -> None:
        self.wait_count = 0

    async def wait(self) -> None:
        self.wait_count += 1


class QuickReplyHarness(TelegramQuickReplyHandlers):
    pass


def _settings() -> Settings:
    return Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        admin_telegram_ids={7},
    )


def _hashtags(text: str) -> list[MessageEntity]:
    entities: list[MessageEntity] = []
    cursor = 0
    for part in text.split():
        offset = text.index(part, cursor)
        cursor = offset + len(part)
        if part.startswith("#"):
            entities.append(
                MessageEntity(
                    type=MessageEntityType.HASHTAG,
                    offset=offset,
                    length=len(part),
                )
            )
    return entities


def _message(
    *,
    text: str,
    message_id: int = 301,
    topic_id: int = 777,
    status_message_id: int = 901,
) -> AsyncMock:
    message = AsyncMock(spec=Message)
    message.message_id = message_id
    message.message_thread_id = topic_id
    message.chat = SimpleNamespace(id=-100123)
    message.from_user = SimpleNamespace(
        id=7,
        is_bot=False,
        full_name="Operator",
        username="operator",
    )
    message.text = text
    message.entities = _hashtags(text)
    message.reply = AsyncMock(return_value=SimpleNamespace(message_id=status_message_id))
    return message


def _bot() -> SimpleNamespace:
    return SimpleNamespace(
        delete_message=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)),
        edit_message_text=AsyncMock(),
        pin_chat_message=AsyncMock(),
    )


def _harness(service: QuickReplyService, bot: SimpleNamespace) -> QuickReplyHarness:
    harness = QuickReplyHarness()
    harness.bot = bot
    harness.settings = _settings()
    harness.authorization = AuthorizationService(harness.settings)
    harness.limiter = FakeLimiter()  # type: ignore[assignment]
    harness.quick_reply_service = service
    harness.quick_replies_topic_id = 777
    harness.recover_quick_replies_topic = None
    harness.initialize_quick_reply_runtime()
    return harness


async def test_valid_quick_response_is_saved_unchanged(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/valid-quick-response.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        bot = _bot()
        harness = _harness(service, bot)
        message = _message(text="Переустановите TikTok #TikTok #Android #VPN #Инструкция")

        assert await harness.handle_quick_reply_topic_message(message) is True

        saved = await service.get_by_source(
            source_chat_id=-100123,
            source_message_id=301,
        )
        assert saved is not None
        assert saved.state == QUICK_RESPONSE_VALID
        assert saved.text == message.text
        assert saved.tags == ("#TikTok", "#Android", "#VPN", "#Инструкция")
        assert saved.status_message_id == 901
        message.reply.assert_awaited_once_with(QUICK_RESPONSE_SAVED_TEXT, parse_mode=None)
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_invalid_response_gets_exact_warning_and_edit_makes_it_valid(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/corrected-quick-response.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        bot = _bot()
        harness = _harness(service, bot)
        message = _message(text="Текст #1 #2 #3 #4 #5")

        await harness.handle_quick_reply_topic_message(message)

        pending = await service.get_by_source(
            source_chat_id=-100123,
            source_message_id=301,
        )
        assert pending is not None
        assert pending.state == QUICK_RESPONSE_PENDING_DELETION
        assert pending.status_message_id == 901
        message.reply.assert_awaited_once_with(
            QUICK_RESPONSE_WARNING_TEXT,
            parse_mode=None,
        )

        message.text = "Исправленный текст #1 #2 #3 #4"
        message.entities = _hashtags(message.text)
        await harness.handle_quick_reply_topic_message(message)

        corrected = await service.get_by_source(
            source_chat_id=-100123,
            source_message_id=301,
        )
        assert corrected is not None
        assert corrected.state == QUICK_RESPONSE_VALID
        assert corrected.status_message_id == 901
        bot.edit_message_text.assert_awaited_with(
            chat_id=-100123,
            message_id=901,
            text=QUICK_RESPONSE_SAVED_TEXT,
            parse_mode=None,
        )
        assert message.reply.await_count == 1
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_invalid_response_and_warning_are_deleted_after_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "suppsystem.telegram_quick_replies.QUICK_RESPONSE_DELETE_DELAY_SECONDS",
        0,
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/expired-quick-response.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        bot = _bot()
        harness = _harness(service, bot)
        message = _message(text="Текст #1 #2 #3 #4 #5")

        await harness.handle_quick_reply_topic_message(message)
        await asyncio.sleep(0.05)

        assert (
            await service.get_by_source(
                source_chat_id=-100123,
                source_message_id=301,
            )
            is None
        )
        assert bot.delete_message.await_args_list == [
            call(chat_id=-100123, message_id=301),
            call(chat_id=-100123, message_id=901),
        ]
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_message_outside_quick_response_topic_is_not_consumed(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/other-topic.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        harness = _harness(service, _bot())

        assert (
            await harness.handle_quick_reply_topic_message(
                _message(text="Обычный ответ", topic_id=778)
            )
            is False
        )
        assert await service.list_valid() == []
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_instruction_is_plain_pinned_message_without_buttons(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-response-instruction.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        bot = _bot()
        harness = _harness(service, bot)

        await harness.ensure_quick_response_topic()

        bot.send_message.assert_awaited_once_with(
            chat_id=-100123,
            message_thread_id=777,
            text=QUICK_RESPONSE_INSTRUCTION_TEXT,
            parse_mode=None,
        )
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100123,
            message_id=501,
            disable_notification=True,
        )
        assert await service.instruction_message_id(-100123) == 501
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_deleted_topic_is_recreated_and_valid_responses_are_restored(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-response-recovery.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        saved = await service.save_valid(
            text="Сохранённый ответ #VPN",
            tags=["#VPN"],
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=401,
        )
        await service.save_instruction_message_id(-100123, 500, 777)
        missing_topic = TelegramBadRequest(
            method=EditMessageText(
                chat_id=-100123,
                message_id=500,
                text=QUICK_RESPONSE_INSTRUCTION_TEXT,
            ),
            message="Bad Request: message thread not found",
        )
        bot = _bot()
        bot.edit_message_text = AsyncMock(side_effect=missing_topic)
        bot.send_message = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=501),
                SimpleNamespace(message_id=502),
                SimpleNamespace(message_id=503),
            ]
        )
        harness = _harness(service, bot)
        recover = AsyncMock(return_value=888)
        harness.recover_quick_replies_topic = recover

        await harness.ensure_quick_response_topic()

        recover.assert_awaited_once_with(777)
        assert harness.quick_replies_topic_id == 888
        assert [item.kwargs["message_thread_id"] for item in bot.send_message.await_args_list] == [
            888,
            888,
            888,
        ]
        assert bot.send_message.await_args_list[1].kwargs["text"] == "Сохранённый ответ #VPN"
        assert bot.send_message.await_args_list[2].kwargs["text"] == QUICK_RESPONSE_SAVED_TEXT
        restored = await service.get_by_source(
            source_chat_id=-100123,
            source_message_id=401,
        )
        assert restored is not None
        assert restored.id == saved.id
        assert restored.published_message_id == 502
        assert restored.status_message_id == 503
        assert await service.instruction_message_id(-100123) == 501
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_topic_recovered_before_adapter_start_restores_responses(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/pre-recovered-topic.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        await service.save_valid(
            text="Ответ переживёт рестарт #VPN",
            tags=["#VPN"],
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=401,
        )
        await service.save_instruction_message_id(-100123, 500, 777)
        bot = _bot()
        bot.send_message = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=501),
                SimpleNamespace(message_id=502),
                SimpleNamespace(message_id=503),
            ]
        )
        harness = _harness(service, bot)
        harness.quick_replies_topic_id = 888

        await harness.ensure_quick_response_topic()

        bot.edit_message_text.assert_not_awaited()
        assert [item.kwargs["message_thread_id"] for item in bot.send_message.await_args_list] == [
            888,
            888,
            888,
        ]
        assert bot.send_message.await_args_list[1].kwargs["text"] == (
            "Ответ переживёт рестарт #VPN"
        )
        assert bot.send_message.await_args_list[2].kwargs["text"] == QUICK_RESPONSE_SAVED_TEXT
        assert await service.instruction_topic_id(-100123) == 888
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_pending_expirations_are_restored_after_restart(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/restart-expiration.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        await service.save_pending_deletion(
            text="Текст #1 #2 #3 #4 #5",
            tags=["#1", "#2", "#3", "#4", "#5"],
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=301,
            invalid_until=datetime.now(UTC),
        )
        bot = _bot()
        harness = _harness(service, bot)

        await harness.restore_pending_quick_response_expirations()
        await asyncio.sleep(0.05)

        bot.delete_message.assert_awaited_with(chat_id=-100123, message_id=301)
    finally:
        await harness.shutdown_quick_reply_runtime()
        await database.dispose()


async def test_quick_response_topic_worker_refreshes_and_stops() -> None:
    refreshed = asyncio.Event()

    async def ensure_topic() -> None:
        refreshed.set()

    topic = SimpleNamespace(ensure_quick_response_topic=ensure_topic)
    worker = QuickResponseTopicRefreshWorker(
        topic,  # type: ignore[arg-type]
        interval_seconds=0.01,
    )
    task = asyncio.create_task(worker.run())

    await asyncio.wait_for(refreshed.wait(), timeout=1)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)
