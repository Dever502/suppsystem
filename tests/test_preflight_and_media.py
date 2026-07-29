from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from supportbot.__main__ import validate_api_settings, validate_support_group
from supportbot.authorization import AuthorizationService
from supportbot.config import Settings
from supportbot.models import TicketStatus
from supportbot.panel import PanelActionResult
from supportbot.service_types import InternalNoteView, TopicProvisioningConflictError
from supportbot.telegram_adapter import (
    GIFT_DAYS_ERROR_TEXT,
    SUPPORT_PENDING_TEXT,
    TICKET_CLOSED_TEXT,
    TOPIC_COMMANDS,
    TelegramSupportAdapter,
    TicketLockPool,
)
from supportbot.telegram_errors import is_missing_topic_error
from supportbot.telegram_formatting import (
    internal_notes_text,
    operator_ticket_info,
    panel_action_reply,
    topic_name,
)
from supportbot.telegram_message_utils import (
    media_metadata,
    message_command,
    rating_keyboard,
    rating_report,
)


class FakeBot:
    async def get_chat(self, support_group_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=support_group_id,
            type="supergroup",
            title="Support",
            is_forum=True,
        )

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=42)

    async def get_chat_member(self, support_group_id: int, user_id: int) -> SimpleNamespace:
        return SimpleNamespace(status="administrator", can_manage_topics=True)


class FakeBotWithoutTopicPermission(FakeBot):
    async def get_chat_member(self, support_group_id: int, user_id: int) -> SimpleNamespace:
        return SimpleNamespace(status="administrator", can_manage_topics=False)


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: TOPIC_ID_INVALID",
        "Bad Request: message thread not found",
        "Bad Request: message thread is not found",
    ],
)
def test_missing_topic_errors_are_recognized(message: str) -> None:
    assert is_missing_topic_error(RuntimeError(message)) is True


def test_unrelated_bad_request_is_not_a_missing_topic() -> None:
    assert is_missing_topic_error(RuntimeError("Bad Request: message is too long")) is False


async def test_support_group_preflight_accepts_forum_admin() -> None:
    await validate_support_group(FakeBot(), -100123)  # type: ignore[arg-type]


async def test_support_group_preflight_rejects_missing_topic_permission() -> None:
    with pytest.raises(RuntimeError, match="manage topics"):
        await validate_support_group(FakeBotWithoutTopicPermission(), -100123)  # type: ignore[arg-type]


def test_media_metadata_includes_document_file_fields() -> None:
    message = SimpleNamespace(
        content_type="document",
        document=SimpleNamespace(
            file_id="file-id",
            file_unique_id="unique-id",
            file_size=1234,
            file_name="report.pdf",
            mime_type="application/pdf",
        ),
    )

    assert media_metadata(message) == {  # type: ignore[arg-type]
        "telegram_content_type": "document",
        "file_id": "file-id",
        "file_unique_id": "unique-id",
        "file_size": 1234,
        "file_name": "report.pdf",
        "mime_type": "application/pdf",
    }


def test_media_metadata_uses_largest_photo_size() -> None:
    message = SimpleNamespace(
        content_type="photo",
        photo=[
            SimpleNamespace(file_id="small", file_unique_id="small-u", width=90, height=90),
            SimpleNamespace(file_id="large", file_unique_id="large-u", width=1280, height=720),
        ],
    )

    assert media_metadata(message) == {  # type: ignore[arg-type]
        "telegram_content_type": "photo",
        "file_id": "large",
        "file_unique_id": "large-u",
        "width": 1280,
        "height": 720,
        "photo_size_count": 2,
    }


def test_internal_notes_text_is_readable_and_escapes_content() -> None:
    note = InternalNoteView(
        content="Проверить <оплату>",
        operator_telegram_id=42,
        created_at=datetime(2026, 7, 2, 14, 30, tzinfo=UTC),
        operator_display_name="Alice <Operator>",
        operator_username="alice",
    )

    assert internal_notes_text([]) == "📝 <b>Заметки</b>\n\nЗаметок пока нет."
    assert internal_notes_text([note]) == (
        "📝 <b>Заметки</b>\n\n"
        "<code>02.07.2026, 14:30 UTC</code> · Alice &lt;Operator&gt;\n"
        "«Проверить &lt;оплату&gt;»"
    )


@pytest.mark.parametrize(
    ("display_name", "username", "expected_author"),
    [
        ("Alice", "alice", "Alice"),
        (None, "alice", "@alice"),
        (None, None, "<code>TG:42</code>"),
    ],
)
def test_internal_notes_author_uses_name_username_then_telegram_id(
    display_name: str | None, username: str | None, expected_author: str
) -> None:
    note = InternalNoteView(
        content="Заметка",
        operator_telegram_id=42,
        created_at=datetime(2026, 7, 2, 14, 30, tzinfo=UTC),
        operator_display_name=display_name,
        operator_username=username,
    )

    assert (
        f"<code>02.07.2026, 14:30 UTC</code> · {expected_author}\n«Заметка»"
        in internal_notes_text([note])
    )


def test_topic_name_uses_operator_friendly_identity_without_ticket_id() -> None:
    ticket = SimpleNamespace(
        id="4244b623-d894-463c-9753-6fd42160ac48",
        display_name="Ivan Petrov",
        username="ivan",
        telegram_user_id=123456789,
    )

    assert topic_name(ticket, closed=False) == "🔴 Ivan Petrov"  # type: ignore[arg-type]
    assert "4244b623" not in topic_name(ticket, closed=False)  # type: ignore[arg-type]


def test_topic_name_falls_back_to_username_or_telegram_id() -> None:
    username_ticket = SimpleNamespace(
        id="ticket-id",
        display_name=None,
        username="ivan",
        telegram_user_id=123456789,
    )
    id_ticket = SimpleNamespace(
        id="ticket-id",
        display_name=None,
        username=None,
        telegram_user_id=123456789,
    )

    assert topic_name(username_ticket, closed=True) == "🟢 @ivan"  # type: ignore[arg-type]
    assert topic_name(id_ticket, closed=False) == "🔴 TG:123456789"  # type: ignore[arg-type]


def test_operator_ticket_info_is_readable() -> None:
    ticket = SimpleNamespace(
        id="ticket-1",
        status=TicketStatus.OPEN,
        topic_id=777,
        display_name="Ivan Petrov",
        username="ivan",
        telegram_user_id=123456789,
        created_at=datetime(2026, 7, 2, 12, 40, tzinfo=UTC),
        updated_at=datetime(2026, 7, 2, 14, 30, tzinfo=UTC),
        closed_at=None,
    )

    assert operator_ticket_info(ticket) == (  # type: ignore[arg-type]
        "🎫 <b>Тикет</b>\n\n"
        "Статус: 🟢 <code>Открыт</code>\n"
        "ID: <code>ticket-1</code>\n"
        "Тема: <code>777</code>\n\n"
        "👤 <b>Клиент</b>\n\n"
        "<b>Ivan Petrov · @ivan</b>\n"
        "Telegram ID: <code>123456789</code>\n\n"
        "🕒 <b>История</b>\n\n"
        "Создан: <code>02.07.2026, 12:40 UTC</code>\n"
        "Обновлён: <code>02.07.2026, 14:30 UTC</code>\n"
        "Закрыт: <code>—</code>"
    )


def test_rating_messages_use_card_format() -> None:
    ticket = SimpleNamespace(
        id="ticket-1",
        display_name="Ivan <Petrov>",
        username="ivan",
        telegram_user_id=123456789,
    )

    assert TICKET_CLOSED_TEXT == (
        "✅ <b>Обращение закрыто</b>\n\n"
        "Спасибо за обращение. Если вопрос остался, отправьте новое сообщение — "
        "мы снова откроем тикет.\n\n"
        "⭐ <b>Оцените поддержку</b>\n"
        "Выберите оценку ниже:"
    )
    assert rating_report(ticket, 4) == (  # type: ignore[arg-type]
        "⭐ <b>Оценка поддержки</b>\n\n"
        "Оценка: ⭐⭐⭐⭐ <b>4/5</b>\n\n"
        "👤 <b>Клиент</b>\n\n"
        "<b>Ivan &lt;Petrov&gt; · @ivan</b>\n"
        "Telegram ID: <code>123456789</code>"
    )
    keyboard = rating_keyboard("ticket-1", 3)
    assert keyboard.inline_keyboard[0][3].callback_data == "support_rating:ticket-1:3:4"


async def test_ticket_lock_pool_releases_unused_entries() -> None:
    pool = TicketLockPool()
    active = 0
    maximum_active = 0

    async def use_lock() -> None:
        nonlocal active, maximum_active
        async with pool.hold(123):
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(use_lock() for _ in range(20)))
    for key in range(100):
        async with pool.hold(key):
            pass

    assert maximum_active == 1
    assert len(pool) == 0


async def test_customer_card_hides_internal_ticket_id() -> None:
    ticket = SimpleNamespace(
        id="4244b623-d894-463c-9753-6fd42160ac48",
        display_name="Ivan <Petrov>",
        username="ivan",
        telegram_user_id=123456789,
    )

    adapter = object.__new__(TelegramSupportAdapter)
    adapter.panel_service = None

    card = await adapter._customer_card(ticket)  # type: ignore[arg-type]

    assert "4244b623-d894-463c-9753-6fd42160ac48" not in card
    assert "Telegram ID: <code>123456789</code>" in card
    assert "<b>Ivan &lt;Petrov&gt; · @ivan</b>" in card


async def test_send_customer_card_posts_to_support_topic() -> None:
    class FakeLimiter:
        def __init__(self) -> None:
            self.wait_count = 0

        async def wait(self) -> None:
            self.wait_count += 1

    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send_message(self, **kwargs: object) -> None:
            self.messages.append(kwargs)

    ticket = SimpleNamespace(
        id="4244b623-d894-463c-9753-6fd42160ac48",
        display_name="Ivan Petrov",
        username="ivan",
        telegram_user_id=123456789,
        topic_id=777,
    )
    bot = FakeBot()
    limiter = FakeLimiter()
    adapter = object.__new__(TelegramSupportAdapter)
    adapter.bot = bot
    adapter.limiter = limiter
    adapter.panel_service = None
    adapter.settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
    )

    await adapter._send_customer_card(ticket, event="test_customer_card_sent")  # type: ignore[arg-type]

    assert limiter.wait_count == 1
    assert bot.messages == [
        {
            "chat_id": -100123,
            "message_thread_id": 777,
            "text": (
                "👤 <b>Клиент</b>\n\n"
                "<b>Ivan Petrov · @ivan</b>\n"
                "Telegram ID: <code>123456789</code>\n\n"
                "💳 <b>Подписка Remnawave</b>\n\n"
                "Интеграция не подключена."
            ),
        }
    ]


async def test_stale_created_topic_is_deleted_without_changing_ticket_state() -> None:
    class FakeLimiter:
        def __init__(self) -> None:
            self.wait_count = 0

        async def wait(self) -> None:
            self.wait_count += 1

    class FakeBot:
        def __init__(self) -> None:
            self.deleted: list[dict[str, object]] = []

        async def create_forum_topic(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(message_thread_id=944)

        async def delete_forum_topic(self, **kwargs: object) -> None:
            self.deleted.append(kwargs)

    class FakeTicketService:
        def __init__(self, ticket: SimpleNamespace) -> None:
            self.ticket = ticket
            self.attach_tokens: list[str] = []

        async def claim_topic_provisioning(self, ticket_id: str) -> str:
            return "current-token"

        async def get_ticket(self, ticket_id: str) -> SimpleNamespace:
            return self.ticket

        async def attach_topic(
            self, ticket_id: str, topic_id: int, *, token: str
        ) -> SimpleNamespace:
            self.attach_tokens.append(token)
            raise TopicProvisioningConflictError(ticket_id)

    ticket = SimpleNamespace(
        id="ticket-stale-topic",
        telegram_user_id=123,
        display_name="Stale Topic",
        username=None,
        topic_id=None,
        status=TicketStatus.PROVISIONING,
    )
    bot = FakeBot()
    limiter = FakeLimiter()
    ticket_service = FakeTicketService(ticket)
    adapter = object.__new__(TelegramSupportAdapter)
    adapter.bot = bot  # type: ignore[assignment]
    adapter.limiter = limiter
    adapter.ticket_service = ticket_service  # type: ignore[assignment]
    adapter.settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
    )

    current = await adapter._ensure_topic(ticket)  # type: ignore[arg-type]

    assert current is ticket
    assert ticket_service.attach_tokens == ["current-token"]
    assert limiter.wait_count == 2
    assert bot.deleted == [{"chat_id": -100123, "message_thread_id": 944}]


async def test_bindtopic_replaces_uncertain_claim_and_attaches_with_fresh_token() -> None:
    calls: list[tuple[object, ...]] = []
    attached = SimpleNamespace(
        id="ticket-manual-bind",
        telegram_user_id=123,
        topic_id=945,
    )

    class FakeAuthorization:
        def has_full_access(self, telegram_user_id: int) -> bool:
            return telegram_user_id == 7

    class FakeTicketService:
        async def get_ticket(self, ticket_id: str) -> SimpleNamespace:
            calls.append(("get", ticket_id))
            return SimpleNamespace(telegram_user_id=123)

        async def reset_topic_provisioning(self, ticket_id: str) -> bool:
            calls.append(("reset", ticket_id))
            return True

        async def claim_topic_provisioning(self, ticket_id: str) -> str:
            calls.append(("claim", ticket_id))
            return "fresh-manual-token"

        async def attach_topic(
            self, ticket_id: str, topic_id: int, *, token: str
        ) -> SimpleNamespace:
            calls.append(("attach", ticket_id, topic_id, token))
            return attached

    class FakeMessage:
        from_user = SimpleNamespace(id=7)
        text = "/bindtopic ticket-manual-bind"
        message_thread_id = 945

        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, text: str) -> None:
            self.replies.append(text)

    async def sync_ticket_topic(ticket: object) -> bool:
        calls.append(("sync", ticket))
        return True

    adapter = object.__new__(TelegramSupportAdapter)
    adapter.authorization = FakeAuthorization()  # type: ignore[assignment]
    adapter.ticket_service = FakeTicketService()  # type: ignore[assignment]
    adapter._ticket_locks = TicketLockPool()
    adapter._sync_ticket_topic = sync_ticket_topic  # type: ignore[method-assign]
    message = FakeMessage()

    await adapter._handle_orphan_topic_command(message, "/bindtopic")  # type: ignore[arg-type]

    assert calls == [
        ("get", "ticket-manual-bind"),
        ("reset", "ticket-manual-bind"),
        ("claim", "ticket-manual-bind"),
        ("attach", "ticket-manual-bind", 945, "fresh-manual-token"),
        ("sync", attached),
    ]
    assert message.replies == ["✅ Тема привязана к тикету."]


def test_message_command_uses_caption_for_media_commands() -> None:
    message = SimpleNamespace(text=None, caption="/Tsop please")

    assert message_command(message) == "/tsop"  # type: ignore[arg-type]


def test_panel_action_reply_formats_mutation_results() -> None:
    assert (
        panel_action_reply(
            PanelActionResult(
                action="extend_subscription",
                status="completed",
                changed=True,
                identity_provider="telegram",
                identity_value="123",
                affected_rows=1,
            )
        )
        == "✅ <b>Подписка продлена</b>\nЗатронуто записей: <b>1</b>."
    )
    assert (
        panel_action_reply(
            PanelActionResult(
                action="reset_devices",
                status="completed",
                changed=True,
                identity_provider="telegram",
                identity_value="123",
                devices_removed=2,
            )
        )
        == "✅ <b>Устройства сброшены</b>\nУдалено устройств: <b>2</b>."
    )
    assert (
        panel_action_reply(
            PanelActionResult(
                action="reset_key",
                status="duplicate",
                changed=False,
                identity_provider="telegram",
                identity_value="123",
            )
        )
        == "ℹ️ Команда уже обработана."
    )


def test_topic_command_allowlist_contains_close_variants() -> None:
    assert "/stop" in TOPIC_COMMANDS
    assert "/hidestop" in TOPIC_COMMANDS
    assert "/info" in TOPIC_COMMANDS
    assert "/subinfo" in TOPIC_COMMANDS
    assert "/gift" in TOPIC_COMMANDS
    assert "/resetkey" in TOPIC_COMMANDS
    assert "/revokelink" in TOPIC_COMMANDS
    assert "/resetdevices" in TOPIC_COMMANDS
    assert "/resolvepanel" in TOPIC_COMMANDS
    assert "/note" in TOPIC_COMMANDS
    assert "/notes" in TOPIC_COMMANDS
    assert "/tsop" not in TOPIC_COMMANDS


def test_gift_validation_error_is_operator_friendly() -> None:
    assert GIFT_DAYS_ERROR_TEXT == (
        "⚠️ <b>Неверное количество дней</b>\n\n"
        "Укажите целое число от 1 до 9999. Пример: <code>/gift 30</code>"
    )


def _api_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "support_bot_token": SecretStr("test-token"),
        "support_group_id": -100123,
        "api_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_api_settings_require_token_by_default() -> None:
    with pytest.raises(ValueError, match="API_ADMIN_TOKEN"):
        validate_api_settings(_api_settings())


def test_api_settings_allow_token_authenticated_remote_bind() -> None:
    validate_api_settings(
        _api_settings(
            api_host="0.0.0.0",
            api_admin_token=SecretStr("0123456789abcdef0123456789abcdef"),
        )
    )


def test_api_settings_allow_unsafe_auth_disable_on_loopback() -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        validate_api_settings(_api_settings(api_host=host, api_unsafe_disable_auth=True))


def test_api_settings_reject_unsafe_auth_disable_on_remote_bind() -> None:
    with pytest.raises(RuntimeError, match="loopback"):
        validate_api_settings(_api_settings(api_host="0.0.0.0", api_unsafe_disable_auth=True))


async def test_reopened_ticket_customer_card_is_best_effort() -> None:
    ticket = SimpleNamespace(
        id="ticket-1",
        telegram_user_id=123456789,
        topic_id=777,
    )
    adapter = object.__new__(TelegramSupportAdapter)
    calls = {"sync": 0, "card": 0}

    async def sync_ticket_topic(ticket_arg: object) -> bool:
        calls["sync"] += 1
        return True

    async def send_customer_card(ticket_arg: object, *, event: str) -> None:
        calls["card"] += 1
        raise RuntimeError("card failed")

    adapter._sync_ticket_topic = sync_ticket_topic  # type: ignore[method-assign]
    adapter._send_customer_card = send_customer_card  # type: ignore[method-assign]

    await adapter._refresh_reopened_ticket_context(ticket)  # type: ignore[arg-type]

    assert calls == {"sync": 1, "card": 1}


async def test_private_message_is_persisted_before_topic_provisioning() -> None:
    calls: list[str] = []

    class FakeTicketService:
        async def accept_customer_message(self, **kwargs: object) -> SimpleNamespace:
            calls.append("persist")
            assert kwargs["target_chat_id"] == -100123
            ticket = SimpleNamespace(
                id="ticket-1",
                telegram_user_id=123,
                topic_id=None,
            )
            return SimpleNamespace(changed=True, blocked=False, ticket=ticket, reopened=False)

    class FakeMessage:
        text = "Need help"
        caption = None
        content_type = "text"
        message_id = 10
        chat = SimpleNamespace(id=123)
        from_user = SimpleNamespace(id=123, full_name="Test User", username="test")

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def answer(self, text: str) -> None:
            self.answers.append(text)

    async def fail_topic_provisioning(ticket: object) -> None:
        calls.append("provision")
        raise RuntimeError("Telegram unavailable")

    adapter = object.__new__(TelegramSupportAdapter)
    adapter.ticket_service = FakeTicketService()  # type: ignore[assignment]
    adapter.settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
    )
    adapter._ticket_locks = TicketLockPool()
    adapter._ensure_topic = fail_topic_provisioning  # type: ignore[method-assign]
    message = FakeMessage()

    await adapter.handle_private_message(message)  # type: ignore[arg-type]

    assert calls == ["persist", "provision"]
    assert message.answers == [SUPPORT_PENDING_TEXT]


async def test_readonly_operator_can_list_internal_notes() -> None:
    note = InternalNoteView(
        content="Проверить оплату",
        operator_telegram_id=42,
        created_at=datetime(2026, 7, 2, 14, 30, tzinfo=UTC),
    )

    class FakeTicketService:
        async def get_by_topic(self, topic_id: int) -> SimpleNamespace:
            assert topic_id == 777
            return SimpleNamespace(id="ticket-1")

        async def list_internal_notes(self, ticket_id: str) -> list[InternalNoteView]:
            assert ticket_id == "ticket-1"
            return [note]

    class ReadonlyMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=3, is_bot=False)
            self.message_thread_id = 777
            self.chat = SimpleNamespace(id=-100123)
            self.message_id = 49
            self.text = "/notes"
            self.caption = None
            self.replies: list[str] = []

        async def reply(self, text: str) -> None:
            self.replies.append(text)

    adapter = object.__new__(TelegramSupportAdapter)
    adapter.authorization = AuthorizationService(
        Settings(
            support_bot_token=SecretStr("test-token"),
            support_group_id=-100123,
            readonly_operator_telegram_ids={3},
        )
    )
    adapter.ticket_service = FakeTicketService()  # type: ignore[assignment]
    message = ReadonlyMessage()

    await adapter.handle_group_message(message)  # type: ignore[arg-type]

    assert message.replies == [internal_notes_text([note])]


async def test_readonly_operator_message_is_rejected_before_ticket_service() -> None:
    class ReadonlyMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=3, is_bot=False)
            self.message_thread_id = 777
            self.chat = SimpleNamespace(id=-100123)
            self.message_id = 50
            self.text = "Ответ клиенту"
            self.caption = None
            self.replies: list[str] = []

        async def reply(self, text: str) -> None:
            self.replies.append(text)

    class TicketServiceMustNotBeCalled:
        async def get_by_topic(self, topic_id: int) -> None:
            raise AssertionError("read-only message reached ticket service")

    adapter = object.__new__(TelegramSupportAdapter)
    adapter.authorization = AuthorizationService(
        Settings(
            support_bot_token=SecretStr("test-token"),
            support_group_id=-100123,
            readonly_operator_telegram_ids={3},
        )
    )
    adapter.ticket_service = TicketServiceMustNotBeCalled()  # type: ignore[assignment]
    message = ReadonlyMessage()

    await adapter.handle_group_message(message)  # type: ignore[arg-type]

    assert message.replies == ["⛔ Роль только для чтения. Действие не выполнено."]


@pytest.mark.parametrize(("telegram_id", "allowed"), [(1, True), (2, False)])
async def test_only_full_admin_can_resolve_inconclusive_panel_action(
    telegram_id: int, allowed: bool
) -> None:
    calls: list[dict[str, object]] = []

    class FakeTicketService:
        async def get_by_topic(self, topic_id: int) -> SimpleNamespace:
            assert topic_id == 777
            return SimpleNamespace(id="ticket-1")

    class FakePanelService:
        async def resolve_inconclusive_action(self, **kwargs: object) -> bool:
            calls.append(kwargs)
            return True

    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=telegram_id, is_bot=False)
            self.message_thread_id = 777
            self.chat = SimpleNamespace(id=-100123)
            self.message_id = 51
            self.text = "/resolvepanel action-uuid applied"
            self.caption = None
            self.replies: list[str] = []

        async def reply(self, text: str) -> None:
            self.replies.append(text)

    adapter = object.__new__(TelegramSupportAdapter)
    adapter.authorization = AuthorizationService(
        Settings(
            support_bot_token=SecretStr("test-token"),
            support_group_id=-100123,
            full_admin_telegram_ids={1},
            operator_telegram_ids={2},
        )
    )
    adapter.ticket_service = FakeTicketService()  # type: ignore[assignment]
    adapter.panel_service = FakePanelService()  # type: ignore[assignment]
    message = FakeMessage()

    await adapter.handle_group_message(message)  # type: ignore[arg-type]

    assert bool(calls) is allowed
    if allowed:
        assert calls == [
            {
                "ticket_id": "ticket-1",
                "operator_action_id": "action-uuid",
                "operator_telegram_id": 1,
                "resolution": "applied",
                "idempotency_key": "telegram:-100123:51:/resolvepanel",
            }
        ]
    else:
        assert message.replies == ["⛔ Только full admin может разрешить неизвестный результат."]


def test_migration_url_conversion_supports_async_postgres() -> None:
    from supportbot.migrations import synchronous_database_url

    assert (
        synchronous_database_url("postgresql+asyncpg://user:pass@postgres:5432/support")
        == "postgresql+psycopg://user:pass@postgres:5432/support"
    )
    assert (
        synchronous_database_url("sqlite+aiosqlite:///./data/support.db")
        == "sqlite:///./data/support.db"
    )
