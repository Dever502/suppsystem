from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import Message
from pydantic import SecretStr

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.quick_replies import QuickReplyService, QuickReplyView
from suppsystem.telegram_quick_replies import (
    TelegramQuickReplyHandlers,
    parse_add_answer_argument,
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


def _reply(*, text: str) -> QuickReplyView:
    return QuickReplyView(
        id=11,
        title="AI не отвечает",
        text=text,
        created_by_telegram_id=7,
        created_by_display_name="Operator",
        created_by_username="operator",
        published_message_id=None,
        active=True,
        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )


def test_add_answer_parser_requires_title_and_multiline_text() -> None:
    assert parse_add_answer_argument("AI не отвечает\nПерезапустите приложение.") == (
        "AI не отвечает",
        "Перезапустите приложение.",
    )
    assert parse_add_answer_argument("Только заголовок") is None
    assert parse_add_answer_argument("\nТолько текст") is None
    assert parse_add_answer_argument("Заголовок\n") is None
    assert parse_add_answer_argument(f"{'😀' * 33}\nТекст") is None
    assert parse_add_answer_argument(f"Заголовок\n{'😀' * 1751}") is None


async def test_preview_uses_native_copy_for_short_text_and_message_for_long_text() -> None:
    harness = QuickReplyHarness()

    _, short_keyboard = await harness._quick_reply_preview(7, _reply(text="Короткий ответ"), 0)
    copy_button = short_keyboard.inline_keyboard[0][0]
    assert copy_button.copy_text is not None
    assert copy_button.copy_text.text == "Короткий ответ"

    _, emoji_keyboard = await harness._quick_reply_preview(7, _reply(text="😀" * 128), 0)
    assert emoji_keyboard.inline_keyboard[0][0].copy_text is not None

    long_text = "😀" * 129
    _, long_keyboard = await harness._quick_reply_preview(7, _reply(text=long_text), 0)
    long_button = long_keyboard.inline_keyboard[0][0]
    assert long_button.copy_text is None
    assert long_button.callback_data == "suppsystem_answers:text:7:11"


async def test_add_answer_publishes_once_and_answers_opens_personal_catalog(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/telegram-quick-replies.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)),
            delete_message=AsyncMock(),
        )
        harness = QuickReplyHarness()
        harness.bot = bot
        harness.settings = _settings()
        harness.authorization = AuthorizationService(harness.settings)
        harness.limiter = FakeLimiter()  # type: ignore[assignment]
        harness.quick_reply_service = service
        harness.quick_replies_topic_id = 777

        command = SimpleNamespace(
            from_user=SimpleNamespace(
                id=7,
                is_bot=False,
                full_name="Operator",
                username="operator",
            ),
            chat=SimpleNamespace(id=-100123),
            message_id=101,
            message_thread_id=777,
            text="/addanswer AI не отвечает\nПерезапустите приложение.",
            caption=None,
            reply=AsyncMock(),
        )

        assert await harness.handle_quick_reply_topic_message(command, "/addanswer") is True
        assert bot.send_message.await_count == 1
        publication = bot.send_message.await_args.kwargs
        assert publication["message_thread_id"] == 777
        assert "AI не отвечает" in publication["text"]
        assert "Перезапустите приложение." in publication["text"]
        assert bot.delete_message.await_args.kwargs["message_id"] == 101

        stored, total = await service.list_active(offset=0, limit=10)
        assert total == 1
        assert stored[0].published_message_id == 501

        assert await harness.handle_quick_reply_topic_message(command, "/addanswer") is True
        assert bot.send_message.await_count == 1

        answers_command = SimpleNamespace(
            from_user=command.from_user,
            chat=command.chat,
            message_id=102,
            message_thread_id=777,
            text="/answers",
            caption=None,
            reply=AsyncMock(),
        )
        assert await harness.handle_quick_reply_topic_message(answers_command, "/answers") is True
        assert bot.send_message.await_count == 2
        catalog = bot.send_message.await_args.kwargs
        assert catalog["text"].startswith("📚 Готовые ответы")
        first_button = catalog["reply_markup"].inline_keyboard[0][0]
        assert first_button.text == "AI не отвечает"
        assert first_button.callback_data == "suppsystem_answers:view:7:1:0"

        wrong_topic = SimpleNamespace(message_thread_id=778)
        assert await harness.handle_quick_reply_topic_message(wrong_topic, "") is False
    finally:
        await database.dispose()


async def test_quick_reply_callbacks_are_admin_and_owner_bound(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-reply-callbacks.db")
    await database.create_schema_for_tests()
    try:
        harness = QuickReplyHarness()
        harness.bot = SimpleNamespace(delete_message=AsyncMock(), send_message=AsyncMock())
        harness.settings = _settings()
        harness.authorization = AuthorizationService(harness.settings)
        harness.limiter = FakeLimiter()  # type: ignore[assignment]
        harness.quick_reply_service = QuickReplyService(database)
        harness.quick_replies_topic_id = 777

        unauthorized = SimpleNamespace(
            from_user=SimpleNamespace(id=8),
            data="suppsystem_answers:list:8:0",
            message=None,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(unauthorized)
        unauthorized.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)

        wrong_owner = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data="suppsystem_answers:list:8:0",
            message=None,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(wrong_owner)
        wrong_owner.answer.assert_awaited_once_with(
            "Эта панель открыта другим оператором.",
            show_alert=True,
        )

        panel_message = AsyncMock(spec=Message)
        panel_message.message_thread_id = 777
        panel_message.chat = SimpleNamespace(id=-100123)
        panel_message.edit_text = AsyncMock()
        catalog = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data="suppsystem_answers:list:7:0",
            message=panel_message,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(catalog)
        panel_message.edit_text.assert_awaited_once()
        catalog.answer.assert_awaited_once_with()
    finally:
        await database.dispose()
