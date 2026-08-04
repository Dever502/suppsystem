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
    utf16_code_units,
)
from suppsystem.telegram_quick_replies import TelegramQuickReplyHandlers
from suppsystem.telegram_quick_reply_views import (
    inline_reply_description,
    parse_add_answer_argument,
    parse_add_group_argument,
    parse_inline_group_query,
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


def _operator(user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        is_bot=False,
        full_name="Operator",
        username="operator",
    )


def _group(*, name: str = "AI", reply_count: int = 0) -> QuickReplyGroupView:
    return QuickReplyGroupView(
        id=1,
        name=name,
        created_by_telegram_id=7,
        created_by_display_name="Operator",
        created_by_username="operator",
        published_message_id=None,
        active=True,
        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        reply_count=reply_count,
    )


def _reply(
    *,
    text: str,
    reply_id: int = 11,
    title: str = "AI не отвечает",
) -> QuickReplyView:
    return QuickReplyView(
        id=reply_id,
        group_id=1,
        title=title,
        text=text,
        created_by_telegram_id=7,
        created_by_display_name="Operator",
        created_by_username="operator",
        published_message_id=None,
        active=True,
        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )


def _panel_message(message_id: int) -> AsyncMock:
    message = AsyncMock(spec=Message)
    message.message_id = message_id
    message.message_thread_id = 777
    message.chat = SimpleNamespace(id=-100123)
    message.edit_text = AsyncMock()
    return message


def _harness(
    service: QuickReplyService | SimpleNamespace,
    bot: SimpleNamespace,
) -> QuickReplyHarness:
    harness = QuickReplyHarness()
    harness.bot = bot
    harness.settings = _settings()
    harness.authorization = AuthorizationService(harness.settings)
    harness.limiter = FakeLimiter()  # type: ignore[assignment]
    harness.quick_reply_service = service  # type: ignore[assignment]
    harness.quick_replies_topic_id = 777
    harness.initialize_quick_reply_sessions()
    return harness


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


def test_inline_query_marker_and_description_are_strict_and_compact() -> None:
    assert parse_inline_group_query("qr-group-17") == 17
    assert parse_inline_group_query("  qr-group-17 ignored  ") == 17
    assert parse_inline_group_query("qr-group-0") is None
    assert parse_inline_group_query("qr-group-nope") is None
    assert parse_inline_group_query("other-17") is None
    assert inline_reply_description("  Строка 1\n\nСтрока   2  ") == "Строка 1 Строка 2"
    assert inline_reply_description("123456", limit=5) == "1234…"

    label = QuickReplyHarness._group_button_text(_group(name="😀" * 32, reply_count=123))
    assert utf16_code_units(label) <= 64
    assert label.endswith(" · 123")


def test_inline_result_sends_clean_text_and_safe_controls() -> None:
    short = QuickReplyHarness._inline_reply_result(
        7,
        1,
        _reply(text="Короткий ответ"),
    )
    assert short.title == "AI не отвечает"
    assert short.description == "Короткий ответ"
    assert short.input_message_content.message_text == "Короткий ответ"
    assert short.input_message_content.parse_mode is None
    short_rows = short.reply_markup.inline_keyboard
    assert short_rows[0][0].copy_text is not None
    assert short_rows[0][0].copy_text.text == "Короткий ответ"
    assert short_rows[0][1].callback_data == "suppsystem_answers:delete:7"
    assert short_rows[1][0].switch_inline_query_current_chat == "qr-group-1"

    long = QuickReplyHarness._inline_reply_result(
        7,
        1,
        _reply(text="😀" * 129),
    )
    long_rows = long.reply_markup.inline_keyboard
    assert len(long_rows[0]) == 1
    assert long_rows[0][0].callback_data == "suppsystem_answers:delete:7"


async def test_inline_query_is_personal_authorized_and_paginated() -> None:
    service = SimpleNamespace(
        get_active_group=AsyncMock(return_value=_group(reply_count=2)),
        list_active=AsyncMock(
            return_value=(
                [_reply(text="Перезапустите приложение.")],
                2,
            )
        ),
    )
    harness = _harness(service, SimpleNamespace())

    query = SimpleNamespace(
        from_user=_operator(),
        query="qr-group-1",
        offset="",
        answer=AsyncMock(),
    )
    await harness.handle_quick_reply_inline_query(query)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].title == "AI не отвечает"
    query.answer.assert_awaited_once_with(
        results,
        cache_time=0,
        is_personal=True,
        next_offset="1",
    )
    service.list_active.assert_awaited_once_with(group_id=1, offset=0, limit=20)

    unauthorized = SimpleNamespace(
        from_user=_operator(8),
        query="qr-group-1",
        offset="",
        answer=AsyncMock(),
    )
    await harness.handle_quick_reply_inline_query(unauthorized)
    unauthorized.answer.assert_awaited_once_with(
        [],
        cache_time=0,
        is_personal=True,
        next_offset="",
    )

    invalid = SimpleNamespace(
        from_user=_operator(),
        query="",
        offset="",
        answer=AsyncMock(),
    )
    await harness.handle_quick_reply_inline_query(invalid)
    invalid.answer.assert_awaited_once_with(
        [],
        cache_time=0,
        is_personal=True,
        next_offset="",
    )


async def test_groups_and_answers_publish_once_and_shared_menu_opens_inline(
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
            edit_message_text=AsyncMock(),
            delete_message=AsyncMock(),
            pin_chat_message=AsyncMock(),
        )
        harness = _harness(service, bot)
        operator = _operator()

        group_command = SimpleNamespace(
            from_user=operator,
            chat=SimpleNamespace(id=-100123),
            message_id=101,
            message_thread_id=777,
            text="/addgroup AI",
            caption=None,
            reply=AsyncMock(),
        )
        assert await harness.handle_quick_reply_topic_message(group_command, "/addgroup") is True
        assert bot.send_message.await_count == 1
        assert "Группа готовых ответов" in bot.send_message.await_args.kwargs["text"]

        groups, groups_total = await service.list_groups(offset=0, limit=10)
        assert groups_total == 1
        assert groups[0].name == "AI"
        assert groups[0].published_message_id == 501

        assert await harness.handle_quick_reply_topic_message(group_command, "/addgroup") is True
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
        assert await harness.handle_quick_reply_topic_message(answer_command, "/addanswer") is True
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
        assert await harness.handle_quick_reply_topic_message(answers_command, "/answers") is True
        assert bot.send_message.await_count == 3
        menu = bot.send_message.await_args.kwargs
        assert menu["message_thread_id"] == 777
        assert "список ответов откроется только у вас" in menu["text"]
        group_row = menu["reply_markup"].inline_keyboard[0]
        assert group_row[0].text == "📁 AI · 1"
        assert group_row[0].switch_inline_query_current_chat == "qr-group-1"
        assert group_row[0].callback_data is None
        assert group_row[1].callback_data == "suppsystem_answers:add:1:0"
        assert menu["reply_markup"].inline_keyboard[-1][0].callback_data == (
            "suppsystem_answers:new:0"
        )
        assert await service.menu_message_id(-100123) == 503
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100123,
            message_id=503,
            disable_notification=True,
        )

        wrong_topic = SimpleNamespace(message_thread_id=778)
        assert await harness.handle_quick_reply_topic_message(wrong_topic, "") is False
    finally:
        await database.dispose()


async def test_quick_reply_callbacks_protect_shared_and_personal_controls(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-reply-callbacks.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        await service.save_menu_message_id(-100123, 500)
        bot = SimpleNamespace(
            delete_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        harness = _harness(service, bot)

        unauthorized = SimpleNamespace(
            from_user=_operator(8),
            data="suppsystem_answers:menupage:0",
            message=_panel_message(500),
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(unauthorized)
        unauthorized.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)

        stale = SimpleNamespace(
            from_user=_operator(),
            data="suppsystem_answers:menupage:0",
            message=_panel_message(499),
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(stale)
        stale.answer.assert_awaited_once_with(
            "Панель устарела. Используйте актуальную закреплённую.",
            show_alert=True,
        )

        menu_message = _panel_message(500)
        shared = SimpleNamespace(
            from_user=_operator(),
            data="suppsystem_answers:menupage:0",
            message=menu_message,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(shared)
        menu_message.edit_text.assert_awaited_once()
        shared.answer.assert_awaited_once_with()

        wrong_owner = SimpleNamespace(
            from_user=_operator(),
            data="suppsystem_answers:delete:8",
            message=_panel_message(700),
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(wrong_owner)
        wrong_owner.answer.assert_awaited_once_with(
            "Эта панель открыта другим оператором.",
            show_alert=True,
        )

        personal_message = _panel_message(701)
        delete = SimpleNamespace(
            from_user=_operator(),
            data="suppsystem_answers:delete:7",
            message=personal_message,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(delete)
        bot.delete_message.assert_awaited_once_with(
            chat_id=-100123,
            message_id=701,
        )
        delete.answer.assert_awaited_once_with()
    finally:
        await harness.shutdown_quick_reply_sessions()
        await database.dispose()


async def test_quick_reply_menu_is_persisted_refreshed_and_pinned(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-reply-menu.db")
    await database.create_schema_for_tests()
    try:
        service = QuickReplyService(database)
        group = await service.create_group(
            name="AI",
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=401,
        )
        await service.create(
            group_id=group.group.id,
            title="AI не отвечает",
            text="Перезапустите приложение.",
            operator_telegram_id=7,
            operator_display_name="Operator",
            operator_username="operator",
            source_chat_id=-100123,
            source_message_id=402,
        )
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)),
            pin_chat_message=AsyncMock(),
            delete_message=AsyncMock(),
        )
        harness = _harness(service, bot)

        await harness.ensure_quick_reply_menu()

        bot.send_message.assert_awaited_once()
        created = bot.send_message.await_args.kwargs
        assert created["message_thread_id"] == 777
        group_row = created["reply_markup"].inline_keyboard[0]
        assert group_row[0].text == "📁 AI · 1"
        assert group_row[0].switch_inline_query_current_chat == "qr-group-1"
        assert group_row[1].text == "➕"
        assert group_row[1].callback_data == "suppsystem_answers:add:1:0"
        assert created["reply_markup"].inline_keyboard[-1][0].text == ("➕ Создать новую группу")
        assert await service.menu_message_id(-100123) == 501
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=-100123,
            message_id=501,
            disable_notification=True,
        )

        await harness.ensure_quick_reply_menu()

        assert bot.send_message.await_count == 1
        bot.edit_message_text.assert_awaited_once()
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 501
        assert bot.pin_chat_message.await_count == 2
    finally:
        await database.dispose()


async def test_button_wizard_uses_one_temporary_form_and_direct_group_actions(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/quick-reply-wizard.db")
    await database.create_schema_for_tests()
    harness = QuickReplyHarness()
    try:
        service = QuickReplyService(database)
        await service.save_menu_message_id(-100123, 500)
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    SimpleNamespace(message_id=600),
                    SimpleNamespace(message_id=601),
                    SimpleNamespace(message_id=602),
                ]
            ),
            edit_message_text=AsyncMock(),
            delete_message=AsyncMock(),
            pin_chat_message=AsyncMock(),
        )
        harness = _harness(service, bot)
        operator = _operator()

        open_new = SimpleNamespace(
            from_user=operator,
            data="suppsystem_answers:new:0",
            message=_panel_message(500),
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(open_new)

        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["text"] == "➕ Открываю форму…"
        assert harness._quick_reply_drafts[7].step == "group"
        assert harness._quick_reply_drafts[7].panel_message_id == 600
        first_form = bot.edit_message_text.await_args.kwargs
        assert first_form["message_id"] == 600
        assert "Напишите название группы обычным сообщением" in first_form["text"]
        open_new.answer.assert_awaited_once_with()

        selected_inline = SimpleNamespace(
            message_thread_id=777,
            via_bot=SimpleNamespace(id=42),
        )
        assert await harness.handle_quick_reply_topic_message(selected_inline, "") is True
        assert harness._quick_reply_drafts[7].step == "group"

        group_name = SimpleNamespace(
            from_user=operator,
            chat=SimpleNamespace(id=-100123),
            message_id=701,
            message_thread_id=777,
            text="НЕ РАБОТАЕТ AI",
            caption=None,
            reply=AsyncMock(),
        )
        assert await harness.handle_quick_reply_topic_message(group_name, "") is True
        assert bot.send_message.await_count == 2
        assert "Группа готовых ответов" in bot.send_message.await_args.kwargs["text"]
        assert harness._quick_reply_drafts[7].step == "title"
        title_form = bot.edit_message_text.await_args.kwargs
        assert title_form["message_id"] == 600
        assert "Шаг 1 из 2" in title_form["text"]

        title = SimpleNamespace(
            from_user=operator,
            chat=group_name.chat,
            message_id=702,
            message_thread_id=777,
            text="AI не отвечает",
            caption=None,
            reply=AsyncMock(),
        )
        assert await harness.handle_quick_reply_topic_message(title, "") is True
        assert bot.send_message.await_count == 2
        assert harness._quick_reply_drafts[7].step == "text"
        text_form = bot.edit_message_text.await_args.kwargs
        assert text_form["message_id"] == 600
        assert "Шаг 2 из 2" in text_form["text"]

        answer_text = SimpleNamespace(
            from_user=operator,
            chat=group_name.chat,
            message_id=703,
            message_thread_id=777,
            text="Перезапустите приложение и попробуйте ещё раз.",
            caption=None,
            reply=AsyncMock(),
        )
        assert await harness.handle_quick_reply_topic_message(answer_text, "") is True
        confirmation = bot.edit_message_text.await_args.kwargs
        assert confirmation["message_id"] == 600
        assert "Новый готовый ответ" in confirmation["text"]
        assert "Группа: НЕ РАБОТАЕТ AI" in confirmation["text"]
        assert "Название: AI не отвечает" in confirmation["text"]
        assert confirmation["reply_markup"].inline_keyboard[0][0].text == "✅ Сохранить"

        panel = _panel_message(600)
        save = SimpleNamespace(
            from_user=operator,
            data="suppsystem_answers:draftsave:7",
            message=panel,
            answer=AsyncMock(),
        )
        await harness.handle_quick_reply_callback(save)

        assert bot.send_message.await_count == 3
        saved_text = panel.edit_text.await_args.args[0]
        assert saved_text.startswith("✅ Готовый ответ сохранён")
        saved_keyboard = panel.edit_text.await_args.kwargs["reply_markup"]
        assert saved_keyboard.inline_keyboard[1][1].switch_inline_query_current_chat == (
            "qr-group-1"
        )
        save.answer.assert_awaited_once_with("Готовый ответ сохранён.")

        groups, group_total = await service.list_groups(offset=0, limit=10)
        assert group_total == 1
        assert groups[0].name == "НЕ РАБОТАЕТ AI"
        assert groups[0].reply_count == 1
        replies, reply_total = await service.list_active(
            group_id=groups[0].id,
            offset=0,
            limit=10,
        )
        assert reply_total == 1
        assert replies[0].title == "AI не отвечает"
        assert replies[0].text == "Перезапустите приложение и попробуйте ещё раз."
        assert replies[0].published_message_id == 602
        assert not harness._quick_reply_drafts
        assert bot.pin_chat_message.await_count == 2
    finally:
        await harness.shutdown_quick_reply_sessions()
        await database.dispose()
