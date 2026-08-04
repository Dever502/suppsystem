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
from suppsystem.quick_replies import (
    QuickReplyGroupView,
    QuickReplyService,
    QuickReplyView,
)
from suppsystem.telegram_quick_replies import (
    TelegramQuickReplyHandlers,
    parse_add_answer_argument,
    parse_add_group_argument,
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


def _group() -> QuickReplyGroupView:
    return QuickReplyGroupView(
        id=1,
        name="AI",
        created_by_telegram_id=7,
        created_by_display_name="Operator",
        created_by_username="operator",
        published_message_id=None,
        active=True,
        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )


def _reply(*, text: str) -> QuickReplyView:
    return QuickReplyView(
        id=11,
        group_id=1,
        title="AI не отвечает",
        text=text,
        created_by_telegram_id=7,
        created_by_display_name="Operator",
        created_by_username="operator",
        published_message_id=None,
        active=True,
        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )


def test_add_group_parser_accepts_one_short_plain_text_line() -> None:
    assert parse_add_group_argument("  НЕ   РАБОТАЕТ AI  ") == "НЕ РАБОТАЕТ AI"
    assert parse_add_group_argument("") is None
    assert parse_add_group_argument("AI\nДругое") is None
    assert parse_add_group_argument("AI | Другое") is None
    assert parse_add_group_argument("😀" * 33) is None


def test_add_answer_parser_requires_group_title_and_multiline_text() -> None:
    assert parse_add_answer_argument("AI | AI не отвечает\nПерезапустите приложение.") == (
        "AI",
        "AI не отвечает",
        "Перезапустите приложение.",
    )
    assert parse_add_answer_argument("AI не отвечает\nТекст") is None
    assert parse_add_answer_argument("AI | Только заголовок") is None
    assert parse_add_answer_argument("| Заголовок\nТекст") is None
    assert parse_add_answer_argument("AI |\nТекст") is None
    assert parse_add_answer_argument(f"{'😀' * 33} | Заголовок\nТекст") is None
    assert parse_add_answer_argument(f"AI | {'😀' * 33}\nТекст") is None
    assert parse_add_answer_argument(f"AI | Заголовок\n{'😀' * 1751}") is None


async def test_preview_uses_native_copy_for_short_text_and_message_for_long_text() -> None:
    harness = QuickReplyHarness()

    _, short_keyboard = await harness._quick_reply_preview(
        7,
        _group(),
        _reply(text="Короткий ответ"),
        0,
        0,
    )
    copy_button = short_keyboard.inline_keyboard[0][0]
    assert copy_button.copy_text is not None
    assert copy_button.copy_text.text == "Короткий ответ"
    assert short_keyboard.inline_keyboard[1][0].callback_data == "suppsystem_answers:group:7:1:0:0"

    _, emoji_keyboard = await harness._quick_reply_preview(
        7,
        _group(),
        _reply(text="😀" * 128),
        0,
        0,
    )
    assert emoji_keyboard.inline_keyboard[0][0].copy_text is not None

    long_text = "😀" * 129
    _, long_keyboard = await harness._quick_reply_preview(
        7,
        _group(),
        _reply(text=long_text),
        0,
        0,
    )
    long_button = long_keyboard.inline_keyboard[0][0]
    assert long_button.copy_text is None
    assert long_button.callback_data == "suppsystem_answers:text:7:11"


async def test_groups_and_answers_publish_once_and_catalog_has_two_levels(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/telegram-quick-replies.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    SimpleNamespace(message_id=501),
                    SimpleNamespace(message_id=502),
                    SimpleNamespace(message_id=503),
                ]
            ),
            delete_message=AsyncMock(),
        )
        harness = QuickReplyHarness()
        harness.bot = bot
        harness.settings = _settings()
        harness.authorization = AuthorizationService(harness.settings)
        harness.limiter = FakeLimiter()  # type: ignore[assignment]
        harness.quick_reply_service = service
        harness.quick_replies_topic_id = 777

        operator = SimpleNamespace(
            id=7,
            is_bot=False,
            full_name="Operator",
            username="operator",
        )
        group_command = SimpleNamespace(
            from_user=operator,
            chat=SimpleNamespace(id=-100123),
            message_id=101,
            message_thread_id=777,
            text="/addgroup AI",
            caption=None,
            reply=AsyncMock(),
        )
        assert (
            await harness.handle_quick_reply_topic_message(
                group_command,
                "/addgroup",
            )
            is True
        )
        assert bot.send_message.await_count == 1
        assert "Группа готовых ответов" in bot.send_message.await_args.kwargs["text"]

        groups, groups_total = await service.list_groups(offset=0, limit=10)
        assert groups_total == 1
        assert groups[0].name == "AI"
        assert groups[0].published_message_id == 501

        assert (
            await harness.handle_quick_reply_topic_message(
                group_command,
                "/addgroup",
            )
            is True
        )
        assert bot.send_message.await_count == 1

        answer_command = SimpleNamespace(
            from_user=operator,
            chat=group_command.chat,
            message_id=102,
            message_thread_id=777,
            text="/addanswer AI | AI не отвечает\nПерезапустите приложение.",
            caption=None,
            reply=AsyncMock(),
        )
        assert (
            await harness.handle_quick_reply_topic_message(
                answer_command,
                "/addanswer",
            )
            is True
        )
        assert bot.send_message.await_count == 2
        publication = bot.send_message.await_args.kwargs
        assert publication["message_thread_id"] == 777
        assert "📁 AI" in publication["text"]
        assert "AI не отвечает" in publication["text"]
        assert "Перезапустите приложение." in publication["text"]

        stored, total = await service.list_active(
            group_id=groups[0].id,
            offset=0,
            limit=10,
        )
        assert total == 1
        assert stored[0].published_message_id == 502

        answers_command = SimpleNamespace(
            from_user=operator,
            chat=group_command.chat,
            message_id=103,
            message_thread_id=777,
            text="/answers",
            caption=None,
            reply=AsyncMock(),
        )
        assert (
            await harness.handle_quick_reply_topic_message(
                answers_command,
                "/answers",
            )
            is True
        )
        assert bot.send_message.await_count == 3
        catalog = bot.send_message.await_args.kwargs
        assert catalog["text"] == "📚 Готовые ответы\n\nВыберите группу:"
        group_button = catalog["reply_markup"].inline_keyboard[0][0]
        assert group_button.text == "📁 AI"
        assert group_button.callback_data == "suppsystem_answers:group:7:1:0:0"

        answer_text, answer_keyboard = await harness._quick_reply_answers(
            7,
            groups[0],
            0,
            0,
        )
        assert answer_text.startswith("📚 Готовые ответы → AI")
        answer_button = answer_keyboard.inline_keyboard[0][0]
        assert answer_button.text == "AI не отвечает"
        assert answer_button.callback_data == "suppsystem_answers:view:7:1:0:0"

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
        service = QuickReplyService(database)
        harness.quick_reply_service = service
        harness.quick_replies_topic_id = 777

        group = await service.create_group(
            name="AI",
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=401,
        )
        reply = await service.create(
            group_id=group.group.id,
            title="AI не отвечает",
            text="Перезапустите приложение.",
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=402,
        )

        unauthorized = SimpleNamespace(
            from_user=SimpleNamespace(id=8),
            data="suppsystem_answers:groups:8:0",
            message=None,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(unauthorized)
        unauthorized.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)

        wrong_owner = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data="suppsystem_answers:groups:8:0",
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
            data="suppsystem_answers:groups:7:0",
            message=panel_message,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(catalog)
        panel_message.edit_text.assert_awaited_once()
        catalog.answer.assert_awaited_once_with()

        panel_message.edit_text.reset_mock()
        group_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data=f"suppsystem_answers:group:7:{group.group.id}:0:0",
            message=panel_message,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(group_callback)
        edited_answers = panel_message.edit_text.await_args.args[0]
        assert "Готовые ответы → AI" in edited_answers
        group_callback.answer.assert_awaited_once_with()

        panel_message.edit_text.reset_mock()
        view_callback = SimpleNamespace(
            from_user=SimpleNamespace(id=7),
            data=f"suppsystem_answers:view:7:{reply.reply.id}:0:0",
            message=panel_message,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(view_callback)
        edited_preview = panel_message.edit_text.await_args.args[0]
        assert "AI → AI не отвечает" in edited_preview
        assert "Перезапустите приложение." in edited_preview
        view_callback.answer.assert_awaited_once_with()
    finally:
        await database.dispose()
