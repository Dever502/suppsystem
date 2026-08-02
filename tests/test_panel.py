from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from suppsystem.database import Database
from suppsystem.durable_work import MAX_RECONCILIATION_ATTEMPTS, DurableWorkRepository
from suppsystem.models import (
    DeliveryOutbox,
    NotificationOutbox,
    NotificationStatus,
    OperatorAction,
    ReconciliationOutbox,
    SupportBlock,
    Ticket,
    TicketChannel,
    TicketMessage,
    TicketStatus,
    User,
    WorkStatus,
    utcnow,
)
from suppsystem.panel import PanelService
from suppsystem.panel_notifications import (
    gift_notification_text,
    revoke_link_notification_text,
)
from suppsystem.reconciliation import ReconciliationWorker
from suppsystem.remnawave import (
    RemnawaveAmbiguousIdentityError,
    RemnawaveBulkActionResult,
    RemnawaveHwidDeviceResetResult,
    RemnawaveNotFoundError,
    RemnawaveTraffic,
    RemnawaveUnavailableError,
    RemnawaveUnknownOutcomeError,
    RemnawaveUser,
)
from suppsystem.services import TicketView
from suppsystem.telegram_formatting import (
    expiration_text,
    format_subscription_lookup,
    panel_status_text,
)


class FakeRemnawave:
    def __init__(self, result: RemnawaveUser | Exception) -> None:
        self.result = result
        self.telegram_ids: list[int] = []
        self.extend_calls: list[tuple[str, int]] = []
        self.revoke_calls: list[tuple[str, bool]] = []
        self.reset_device_calls: list[str] = []
        self.get_device_calls: list[str] = []

    async def get_user_by_telegram_id(self, telegram_id: int) -> RemnawaveUser:
        self.telegram_ids.append(telegram_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def get_user_by_email(self, email: str) -> RemnawaveUser:
        raise AssertionError("email lookup is not used for Telegram tickets yet")

    async def extend_user_expiration(
        self, *, user_uuid: str, extend_days: int
    ) -> RemnawaveBulkActionResult:
        self.extend_calls.append((user_uuid, extend_days))
        return RemnawaveBulkActionResult(affected_rows=1)

    async def revoke_user_subscription(
        self, *, user_uuid: str, revoke_only_passwords: bool
    ) -> RemnawaveUser:
        self.revoke_calls.append((user_uuid, revoke_only_passwords))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def get_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        self.get_device_calls.append(user_uuid)
        return RemnawaveHwidDeviceResetResult(total=2, devices=[])

    async def reset_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        self.reset_device_calls.append(user_uuid)
        return RemnawaveHwidDeviceResetResult(total=2, devices=[])


class WebRemnawave(FakeRemnawave):
    def __init__(self, result: RemnawaveUser, *, stale_uuid: str | None = None) -> None:
        super().__init__(result)
        self.stale_uuid = stale_uuid
        self.uuids: list[str] = []
        self.emails: list[str] = []

    async def get_user_by_uuid(self, user_uuid: str) -> RemnawaveUser:
        self.uuids.append(user_uuid)
        if user_uuid == self.stale_uuid:
            raise RemnawaveNotFoundError()
        assert isinstance(self.result, RemnawaveUser)
        return self.result

    async def get_user_by_email(self, email: str) -> RemnawaveUser:
        self.emails.append(email)
        assert isinstance(self.result, RemnawaveUser)
        return self.result


class UnknownExtendRemnawave(FakeRemnawave):
    async def extend_user_expiration(
        self, *, user_uuid: str, extend_days: int
    ) -> RemnawaveBulkActionResult:
        self.extend_calls.append((user_uuid, extend_days))
        raise RemnawaveUnknownOutcomeError("timeout after mutation was sent")


class AppliedUnknownExtendRemnawave(UnknownExtendRemnawave):
    async def extend_user_expiration(
        self, *, user_uuid: str, extend_days: int
    ) -> RemnawaveBulkActionResult:
        self.extend_calls.append((user_uuid, extend_days))
        assert isinstance(self.result, RemnawaveUser)
        self.result = replace(
            self.result,
            expire_at=self.result.expire_at + timedelta(days=extend_days),
        )
        raise RemnawaveUnknownOutcomeError("timeout after mutation was applied")


class SequencedUnknownExtendRemnawave(UnknownExtendRemnawave):
    def __init__(
        self,
        initial: RemnawaveUser,
        observations: list[RemnawaveUser],
    ) -> None:
        super().__init__(initial)
        self.lookup_results = [initial, *observations]

    async def get_user_by_telegram_id(self, telegram_id: int) -> RemnawaveUser:
        self.telegram_ids.append(telegram_id)
        return self.lookup_results.pop(0)


class StatefulExtendRemnawave(FakeRemnawave):
    async def extend_user_expiration(
        self, *, user_uuid: str, extend_days: int
    ) -> RemnawaveBulkActionResult:
        self.extend_calls.append((user_uuid, extend_days))
        assert isinstance(self.result, RemnawaveUser)
        self.result = replace(
            self.result,
            expire_at=self.result.expire_at + timedelta(days=extend_days),
        )
        return RemnawaveBulkActionResult(affected_rows=1)


class BlockingExtendRemnawave(FakeRemnawave):
    def __init__(self, result: RemnawaveUser) -> None:
        super().__init__(result)
        self.mutation_started = asyncio.Event()
        self.release_mutation = asyncio.Event()

    async def extend_user_expiration(
        self, *, user_uuid: str, extend_days: int
    ) -> RemnawaveBulkActionResult:
        self.extend_calls.append((user_uuid, extend_days))
        self.mutation_started.set()
        await self.release_mutation.wait()
        return RemnawaveBulkActionResult(affected_rows=1)


class AppliedUnknownResetKeyRemnawave(FakeRemnawave):
    async def revoke_user_subscription(
        self, *, user_uuid: str, revoke_only_passwords: bool
    ) -> RemnawaveUser:
        self.revoke_calls.append((user_uuid, revoke_only_passwords))
        assert isinstance(self.result, RemnawaveUser)
        self.result = replace(self.result, credential_fingerprint="b" * 64)
        raise RemnawaveUnknownOutcomeError("reset-key response was lost")


class AppliedUnknownRevokeLinkRemnawave(FakeRemnawave):
    async def revoke_user_subscription(
        self, *, user_uuid: str, revoke_only_passwords: bool
    ) -> RemnawaveUser:
        self.revoke_calls.append((user_uuid, revoke_only_passwords))
        assert revoke_only_passwords is False
        assert isinstance(self.result, RemnawaveUser)
        self.result = replace(self.result, subscription_url="https://sub.example/new-link")
        raise RemnawaveUnknownOutcomeError("revoke response was lost")


class AppliedUnknownResetDevicesRemnawave(FakeRemnawave):
    def __init__(self, result: RemnawaveUser) -> None:
        super().__init__(result)
        self.device_total = 2

    async def get_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        self.get_device_calls.append(user_uuid)
        return RemnawaveHwidDeviceResetResult(total=self.device_total, devices=[])

    async def reset_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        self.reset_device_calls.append(user_uuid)
        self.device_total = 0
        raise RemnawaveUnknownOutcomeError("reset-devices response was lost")


class UnknownResetDevicesRemnawave(FakeRemnawave):
    async def reset_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        self.reset_device_calls.append(user_uuid)
        raise RemnawaveUnknownOutcomeError("reset-devices outcome is unknown")


def ticket_view() -> TicketView:
    now = datetime(2026, 6, 25, tzinfo=UTC)
    return TicketView(
        id="ticket-1",
        user_id=1,
        telegram_user_id=123456789,
        display_name="Alice",
        username="alice",
        topic_id=777,
        status=TicketStatus.OPEN,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        closed_at=None,
    )


def web_ticket_view(*, binding: str | None = None) -> TicketView:
    now = datetime(2026, 6, 25, tzinfo=UTC)
    return TicketView(
        id="web-ticket-1",
        user_id=2,
        telegram_user_id=None,
        display_name="Web Alice",
        username=None,
        topic_id=778,
        status=TicketStatus.OPEN,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        closed_at=None,
        channel=TicketChannel.WEB,
        email="web@example.com",
        identity_provider="web_external_id",
        identity_value="web-1",
        remnawave_user_uuid=binding,
    )


def remnawave_user() -> RemnawaveUser:
    return RemnawaveUser(
        uuid="11111111-1111-1111-1111-111111111111",
        id=123,
        short_uuid="abc123",
        username="user123",
        status="ACTIVE",
        expire_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        subscription_url="https://sub.example/abc123",
        telegram_id=123456789,
        credential_fingerprint="a" * 64,
        email="user@example.com",
        hwid_device_limit=5,
        traffic=RemnawaveTraffic(
            used_traffic_bytes=1024,
            lifetime_used_traffic_bytes=2048,
            online_at=None,
        ),
    )


async def load_action_and_job(
    database: Database, idempotency_key: str
) -> tuple[OperatorAction, ReconciliationOutbox]:
    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == idempotency_key)
        )
        assert action is not None
        job = await session.scalar(
            select(ReconciliationOutbox).where(ReconciliationOutbox.operator_action_id == action.id)
        )
        assert job is not None
        return action, job


async def add_panel_action(
    database: Database,
    *,
    action: str = "remnawave_extend_subscription",
    key: str,
    payload: dict[str, object] | None = None,
) -> OperatorAction:
    async with database.session() as session:
        operator_action = OperatorAction(
            ticket_id="ticket-1",
            operator_telegram_id=42,
            action=action,
            idempotency_key=key,
            payload=payload or {},
            result="unknown",
        )
        session.add(operator_action)
        await session.commit()
        return operator_action


@pytest.mark.asyncio
async def test_panel_service_resolves_telegram_ticket_subscription() -> None:
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave)

    lookup = await service.get_subscription_for_ticket(ticket_view())

    assert remnawave.telegram_ids == [123456789]
    assert lookup.found is True
    assert lookup.identity_provider == "telegram"
    assert lookup.identity_value == "123456789"
    assert lookup.subscription is not None
    assert lookup.subscription.username == "user123"
    assert lookup.subscription.hwid_device_limit == 5
    assert lookup.subscription.used_traffic_bytes == 1024


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (RemnawaveNotFoundError(), "not_found"),
        (RemnawaveAmbiguousIdentityError(), "ambiguous_identity"),
        (RemnawaveUnavailableError(), "unavailable"),
    ],
    ids=("not-found", "ambiguous-identity", "unavailable"),
)
@pytest.mark.asyncio
async def test_panel_service_maps_lookup_error(error: Exception, expected_status: str) -> None:
    service = PanelService(FakeRemnawave(error))

    lookup = await service.get_subscription_for_ticket(ticket_view())

    assert lookup.status == expected_status
    assert lookup.subscription is None


def test_telegram_adapter_explains_ambiguous_identity() -> None:
    assert panel_status_text("ambiguous_identity") == (
        "найдено несколько пользователей с этим Telegram ID; операция заблокирована"
    )


def test_telegram_adapter_formats_subscription_lookup() -> None:
    from suppsystem.panel import PanelSubscriptionLookup, subscription_info
    from suppsystem.telegram_adapter import TOPIC_COMMANDS

    lookup = PanelSubscriptionLookup(
        status="found",
        identity_provider="telegram",
        identity_value="123456789",
        subscription=subscription_info(remnawave_user()),
    )

    text = format_subscription_lookup(lookup)
    expiration = expiration_text(remnawave_user().expire_at)

    assert "/subinfo" in TOPIC_COMMANDS
    assert text == (
        "💳 <b>Подписка Remnawave</b>\n\n"
        "Пользователь: <code>user123</code>\n"
        "Статус: 🟢 <code>ACTIVE</code>\n"
        f"Действует до: <code>{expiration}</code>\n"
        "Email: <code>user@example.com</code>\n"
        "Telegram ID: <code>123456789</code>\n"
        "Лимит устройств: <code>5</code>\n\n"
        "📊 <b>Активность</b>\n\n"
        "Трафик: <code>1.00 KiB</code>\n"
        "За всё время: <code>2.00 KiB</code>\n"
        "Последний онлайн: <code>—</code>\n\n"
        "🔗 <b>Ссылка подписки</b>\n\n"
        "<code>https://sub.example/abc123</code>"
    )


def test_subscription_lookup_uses_compact_telegram_search_label() -> None:
    from suppsystem.panel import PanelSubscriptionLookup

    text = format_subscription_lookup(
        PanelSubscriptionLookup(
            status="not_found",
            identity_provider="telegram",
            identity_value="802720292",
            subscription=None,
        )
    )

    assert text == (
        "💳 <b>Подписка Remnawave</b>\n\nTG:<code>802720292</code>\nСтатус: пользователь не найден"
    )


@pytest.mark.parametrize(
    ("expire_at", "expected"),
    [
        (datetime(2026, 7, 25, tzinfo=UTC), "25.07.2026 (23 дня)"),
        (datetime(2026, 7, 3, tzinfo=UTC), "03.07.2026 (1 день)"),
        (datetime(2026, 7, 2, tzinfo=UTC), "02.07.2026 (сегодня)"),
        (datetime(2026, 7, 1, tzinfo=UTC), "01.07.2026 (истекла)"),
    ],
)
def test_expiration_text_is_compact(expire_at: datetime, expected: str) -> None:
    now = datetime(2026, 7, 2, 12, tzinfo=UTC)

    assert expiration_text(expire_at, now=now) == expected


@pytest.mark.parametrize(
    ("extend_days", "expected_duration"),
    [
        (1, "Вам добавлен <b>1 день</b> подписки."),
        (2, "Вам добавлено <b>2 дня</b> подписки."),
        (5, "Вам добавлено <b>5 дней</b> подписки."),
        (11, "Вам добавлено <b>11 дней</b> подписки."),
        (21, "Вам добавлен <b>21 день</b> подписки."),
    ],
)
def test_gift_notification_formats_days_and_expiration(
    extend_days: int, expected_duration: str
) -> None:
    text = gift_notification_text(
        extend_days,
        datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )

    assert text == (
        "🎁 <b>Подписка продлена</b>\n\n"
        f"{expected_duration}\n\n"
        "Новая дата окончания: <b>5 августа 2026</b>"
    )


def test_revoke_link_notification_escapes_subscription_url() -> None:
    text = revoke_link_notification_text("https://sub.example/new?x=1&next=<unsafe>")

    assert text == (
        "🔐 <b>Ссылка подписки обновлена</b>\n\n"
        "Старая ссылка больше не работает.\n\n"
        "Новая ссылка:\n"
        "<code>https://sub.example/new?x=1&amp;next=&lt;unsafe&gt;</code>"
    )


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    await db.create_schema_for_tests()
    async with db.session() as session:
        session.add(User(id=1, display_name="Test user", username="test"))
        session.add(
            Ticket(
                id="ticket-1",
                user_id=1,
                topic_id=777,
                status=TicketStatus.OPEN,
            )
        )
        await session.commit()
    yield db
    await db.dispose()


async def add_web_ticket(database: Database, *, binding: str | None = None) -> None:
    async with database.session() as session:
        session.add(User(id=2, display_name="Web user", email="web@example.com"))
        session.add(
            Ticket(
                id="web-ticket-1",
                user_id=2,
                topic_id=778,
                status=TicketStatus.OPEN,
                channel=TicketChannel.WEB,
                remnawave_user_uuid=binding,
            )
        )
        await session.commit()


async def test_web_gift_uses_email_binding_and_polls_notification(database: Database) -> None:
    await add_web_ticket(database)
    remnawave = WebRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)

    result = await service.extend_subscription_for_ticket(
        ticket=web_ticket_view(),
        operator_telegram_id=42,
        extend_days=7,
        idempotency_key="telegram:-100:778:/gift",
    )

    async with database.session() as session:
        ticket = await session.get(Ticket, "web-ticket-1")
        messages = list(
            (
                await session.scalars(
                    select(TicketMessage).where(TicketMessage.ticket_id == "web-ticket-1")
                )
            ).all()
        )
        deliveries = list(
            (
                await session.scalars(
                    select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == "web-ticket-1")
                )
            ).all()
        )
    assert result.completed is True
    assert remnawave.emails == ["web@example.com"]
    assert ticket is not None and ticket.remnawave_user_uuid == remnawave_user().uuid
    assert len(messages) == 1 and "Подписка продлена" in (messages[0].content or "")
    assert deliveries == []


async def test_blocked_web_ticket_suppresses_gift_notification(database: Database) -> None:
    await add_web_ticket(database)
    async with database.session() as session:
        session.add(
            SupportBlock(
                ticket_id="web-ticket-1",
                blocked_by_telegram_id=42,
                reason="abuse",
                source="web",
            )
        )
        await session.commit()
    service = PanelService(WebRemnawave(remnawave_user()), database=database)

    result = await service.extend_subscription_for_ticket(
        ticket=web_ticket_view(),
        operator_telegram_id=42,
        extend_days=7,
        idempotency_key="telegram:-100:778:/gift-blocked",
    )

    async with database.session() as session:
        messages = list(
            (
                await session.scalars(
                    select(TicketMessage).where(TicketMessage.ticket_id == "web-ticket-1")
                )
            ).all()
        )
        deliveries = list(
            (
                await session.scalars(
                    select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == "web-ticket-1")
                )
            ).all()
        )
    assert result.completed is True
    assert messages == []
    assert deliveries == []


async def test_web_stale_uuid_is_cleared_and_rebound_by_exact_email(
    database: Database,
) -> None:
    stale = "22222222-2222-2222-2222-222222222222"
    await add_web_ticket(database, binding=stale)
    remnawave = WebRemnawave(remnawave_user(), stale_uuid=stale)
    service = PanelService(remnawave, database=database)

    lookup = await service.get_subscription_for_ticket(web_ticket_view(binding=stale))

    async with database.session() as session:
        ticket = await session.get(Ticket, "web-ticket-1")
    assert lookup.found is True
    assert remnawave.uuids == [stale]
    assert remnawave.emails == ["web@example.com"]
    assert ticket is not None and ticket.remnawave_user_uuid == remnawave_user().uuid


async def test_web_stale_uuid_action_refreshes_durable_recipient(
    database: Database,
) -> None:
    stale = "22222222-2222-2222-2222-222222222222"
    await add_web_ticket(database, binding=stale)
    remnawave = WebRemnawave(remnawave_user(), stale_uuid=stale)
    service = PanelService(remnawave, database=database)
    key = "telegram:-100:778:/revokelink-stale"

    result = await service.revoke_subscription_link_for_ticket(
        ticket=web_ticket_view(binding=stale),
        operator_telegram_id=42,
        idempotency_key=key,
    )

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == key)
        )
        assert action is not None
        intent = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.operator_action_id == action.id)
        )
    assert result.completed is True
    assert result.identity_provider == "email"
    assert result.identity_value == "web@example.com"
    assert action.payload["identity_provider"] == "email"
    assert action.payload["identity_value"] == "web@example.com"
    assert action.payload["remnawave_uuid"] == remnawave_user().uuid
    assert intent is not None
    assert intent.recipient_identity_provider == "email"
    assert intent.recipient_identity_value == "web@example.com"


@pytest.mark.asyncio
async def test_panel_extend_subscription_audits_and_is_idempotent(database: Database) -> None:
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)

    first = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:1:/gift",
    )
    duplicate = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:1:/gift",
    )

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == "telegram:-100:1:/gift")
        )
        assert action is not None
        notification = await session.scalar(
            select(DeliveryOutbox).where(
                DeliveryOutbox.idempotency_key == f"panel-gift:{action.id}:user-notification"
            )
        )

    assert first.completed is True
    assert first.affected_rows == 1
    assert first.subscription is not None
    assert first.subscription.expire_at == remnawave_user().expire_at + timedelta(days=30)
    assert duplicate.status == "duplicate"
    assert remnawave.telegram_ids == [123456789]
    assert remnawave.extend_calls == [("11111111-1111-1111-1111-111111111111", 30)]
    assert action.action == "remnawave_extend_subscription"
    assert action.result == "completed"
    assert action.payload["extend_days"] == 30
    assert action.payload["remnawave_uuid"] == "11111111-1111-1111-1111-111111111111"
    assert action.payload["new_expire_at"] == "2026-08-24T12:00:00+00:00"
    assert "subscription_url" not in action.payload
    assert notification is not None
    assert notification.payload == {
        "kind": "send_text",
        "target_chat_id": 123456789,
        "text": (
            "🎁 <b>Подписка продлена</b>\n\n"
            "Вам добавлено <b>30 дней</b> подписки.\n\n"
            "Новая дата окончания: <b>24 августа 2026</b>"
        ),
        "parse_mode": "HTML",
    }


@pytest.mark.asyncio
async def test_two_distinct_gift_commands_are_both_applied(database: Database) -> None:
    initial = remnawave_user()
    remnawave = StatefulExtendRemnawave(initial)
    service = PanelService(remnawave, database=database)

    for message_id in (10, 11):
        result = await service.extend_subscription_for_ticket(
            ticket=ticket_view(),
            operator_telegram_id=42,
            extend_days=30,
            idempotency_key=f"telegram:-100:{message_id}:/gift",
        )
        assert result.completed is True

    assert isinstance(remnawave.result, RemnawaveUser)
    assert remnawave.result.expire_at == initial.expire_at + timedelta(days=60)
    assert len(remnawave.extend_calls) == 2


@pytest.mark.asyncio
async def test_panel_reset_key_uses_revoke_only_passwords(database: Database) -> None:
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)

    result = await service.reset_key_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:2:/resetkey",
    )

    assert result.completed is True
    assert remnawave.revoke_calls == [("11111111-1111-1111-1111-111111111111", True)]


@pytest.mark.asyncio
async def test_panel_reset_devices_reports_removed_count(database: Database) -> None:
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)

    result = await service.reset_devices_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:3:/resetdevices",
    )

    assert result.completed is True
    assert result.devices_removed == 2
    assert remnawave.reset_device_calls == ["11111111-1111-1111-1111-111111111111"]


@pytest.mark.asyncio
async def test_panel_action_fails_closed_when_lookup_unavailable(database: Database) -> None:
    remnawave = FakeRemnawave(RemnawaveUnavailableError())
    service = PanelService(remnawave, database=database)

    result = await service.reset_devices_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:4:/resetdevices",
    )

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == "telegram:-100:4:/resetdevices"
            )
        )

    assert result.status == "unavailable"
    assert result.changed is False
    assert remnawave.reset_device_calls == []
    assert action is not None
    assert action.result == "unavailable"


@pytest.mark.asyncio
async def test_panel_extend_validates_days_before_audit(database: Database) -> None:
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)

    result = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=0,
        idempotency_key="telegram:-100:5:/gift",
    )

    async with database.session() as session:
        action_id = await session.scalar(
            select(OperatorAction.id).where(
                OperatorAction.idempotency_key == "telegram:-100:5:/gift"
            )
        )

    assert result.status == "validation_error"
    assert action_id is None
    assert remnawave.telegram_ids == []


@pytest.mark.asyncio
async def test_reconciliation_is_not_claimed_while_mutation_is_in_flight(
    database: Database,
) -> None:
    remnawave = BlockingExtendRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)
    mutation_task = asyncio.create_task(
        service.extend_subscription_for_ticket(
            ticket=ticket_view(),
            operator_telegram_id=42,
            extend_days=30,
            idempotency_key="telegram:-100:60:/gift",
        )
    )

    await asyncio.wait_for(remnawave.mutation_started.wait(), timeout=1)
    claimed = await DurableWorkRepository(database).claim_reconciliation()
    remnawave.release_mutation.set()
    result = await asyncio.wait_for(mutation_task, timeout=1)

    assert claimed is None
    assert result.completed is True


@pytest.mark.asyncio
async def test_panel_queues_and_confirms_applied_gift_after_lost_response(
    database: Database,
) -> None:
    remnawave = AppliedUnknownExtendRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:6:/gift",
    )
    action, job = await load_action_and_job(database, "telegram:-100:6:/gift")
    assert result.status == "unknown"
    assert await service.reconcile_durable_action(action.id, job.payload) is True
    async with database.session() as session:
        action = await session.get(OperatorAction, action.id)
    assert action is not None
    assert action.result == "completed"
    assert action.payload["automatic_reconcile"] == "applied"


@pytest.mark.asyncio
async def test_panel_unlocks_gift_when_delayed_check_shows_no_change(
    database: Database,
) -> None:
    remnawave = UnknownExtendRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    first = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:6:/gift",
    )
    repeat = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:7:/gift",
    )

    action, job = await load_action_and_job(database, "telegram:-100:6:/gift")

    assert first.status == "unknown"
    assert first.changed is False
    assert repeat.status == "needs_reconcile"
    assert remnawave.extend_calls == [
        ("11111111-1111-1111-1111-111111111111", 30),
    ]
    assert action.result == "unknown"
    assert action.payload["automatic_reconcile"] == "queued"
    assert (
        await service.reconcile_durable_action(
            action.id,
            job.payload,
            attempt_count=3,
        )
        is True
    )
    assert (
        await service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=42,
            action="extend_subscription",
            idempotency_key="telegram:-100:8:/gift",
            payload={"extend_days": 30},
        )
        == "reserved"
    )
    async with database.session() as session:
        action = await session.get(OperatorAction, action.id)
    assert action is not None
    assert action.result == "not_applied"
    assert action.payload["automatic_reconcile"] == "not_applied"


@pytest.mark.asyncio
async def test_gift_reconcile_worker_handles_eventual_consistency(database: Database) -> None:
    initial = remnawave_user()
    expected = replace(initial, expire_at=initial.expire_at + timedelta(days=30))
    remnawave = SequencedUnknownExtendRemnawave(initial, [initial, expected, expected])
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:70:/gift",
    )
    action, job = await load_action_and_job(database, "telegram:-100:70:/gift")
    assert result.status == "unknown"
    async with database.session() as session:
        notification = await session.scalar(
            select(DeliveryOutbox).where(
                DeliveryOutbox.idempotency_key == f"panel-gift:{action.id}:user-notification"
            )
        )
    assert notification is None
    assert await service.reconcile_durable_action(action.id, job.payload) is False
    assert await service.reconcile_durable_action(action.id, job.payload) is True
    assert await service.reconcile_durable_action(action.id, job.payload) is True
    async with database.session() as session:
        notifications = list(
            (
                await session.scalars(
                    select(DeliveryOutbox).where(
                        DeliveryOutbox.idempotency_key
                        == f"panel-gift:{action.id}:user-notification"
                    )
                )
            ).all()
        )
    assert len(notifications) == 1
    assert notifications[0].payload["text"] == (
        "🎁 <b>Подписка продлена</b>\n\n"
        "Вам добавлено <b>30 дней</b> подписки.\n\n"
        "Новая дата окончания: <b>24 августа 2026</b>"
    )


@pytest.mark.asyncio
async def test_gift_reconcile_does_not_claim_concurrent_change(database: Database) -> None:
    initial = remnawave_user()
    changed_by_more_than_requested = replace(
        initial,
        expire_at=initial.expire_at + timedelta(days=60),
    )
    remnawave = SequencedUnknownExtendRemnawave(
        initial,
        [changed_by_more_than_requested] * 3,
    )
    service = PanelService(
        remnawave,
        database=database,
        reconcile_delay_seconds=0,
        support_group_id=-100123,
    )

    result = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:71:/gift",
    )

    action, job = await load_action_and_job(database, "telegram:-100:71:/gift")
    assert await service.reconcile_durable_action(action.id, job.payload, attempt_count=1) is False
    assert await service.reconcile_durable_action(action.id, job.payload, attempt_count=2) is False
    assert await service.reconcile_durable_action(action.id, job.payload, attempt_count=3) is True
    async with database.session() as session:
        action = await session.get(OperatorAction, action.id)
        notification = await session.scalar(
            select(DeliveryOutbox).where(
                DeliveryOutbox.idempotency_key
                == f"panel-reconcile:{action.id}:operator-notification"
            )
        )

    assert result.status == "unknown"
    assert result.changed is False
    assert action is not None
    assert action.result == "unknown"
    assert action.payload["automatic_reconcile"] == "inconclusive"
    assert notification is not None
    assert action.id in str(notification.payload["text"])
    assert "/resolvepanel" in str(notification.payload["text"])
    assert notification.payload["parse_mode"] == "HTML"
    assert (
        await service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=42,
            action="extend_subscription",
            idempotency_key="telegram:-100:72:/gift",
            payload={"extend_days": 30},
        )
        == "needs_reconcile"
    )


@pytest.mark.parametrize(
    ("resolution", "expected_result"),
    [("applied", "completed"), ("not_applied", "not_applied")],
)
@pytest.mark.parametrize(
    "action_name",
    [
        "remnawave_extend_subscription",
        "remnawave_reset_key",
        "remnawave_reset_devices",
    ],
)
@pytest.mark.asyncio
async def test_manual_resolution_is_audited_first_writer_wins_and_unblocks(
    database: Database,
    resolution: str,
    expected_result: str,
    action_name: str,
) -> None:
    action_suffix = action_name.removeprefix("remnawave_")
    action = await add_panel_action(
        database,
        action=action_name,
        key=f"inconclusive-{action_suffix}-{resolution}",
        payload={
            "automatic_reconcile": "inconclusive",
            "requires_reconcile": True,
        },
    )

    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)
    command_key = f"manual-resolution-{action_suffix}-{resolution}"
    assert await service.resolve_inconclusive_action(
        ticket_id="ticket-1",
        operator_action_id=action.id,
        operator_telegram_id=7,
        resolution=resolution,
        idempotency_key=command_key,
    )
    assert not await service.resolve_inconclusive_action(
        ticket_id="ticket-1",
        operator_action_id=action.id,
        operator_telegram_id=8,
        resolution="not_applied" if resolution == "applied" else "applied",
        idempotency_key=f"{command_key}-late",
    )
    assert await service.reconcile_durable_action(action.id, {"invalid": True}) is True

    async with database.session() as session:
        resolved = await session.get(OperatorAction, action.id)
        audit = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == command_key)
        )
    assert resolved is not None
    assert resolved.result == expected_result
    assert resolved.completed_at is not None
    assert resolved.payload["automatic_reconcile"] == "inconclusive"
    assert resolved.payload["manual_reconcile"] == resolution
    assert resolved.payload["manual_reconciled_by_telegram_id"] == 7
    assert resolved.payload["manual_reconcile_command_key"] == command_key
    assert audit is not None
    assert audit.action == "resolve_remnawave_action"
    assert audit.payload == {"operator_action_id": action.id, "resolution": resolution}
    assert remnawave.telegram_ids == []
    assert (
        await service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=7,
            action="extend_subscription",
            idempotency_key=f"after-manual-{action_suffix}-{resolution}",
            payload={"extend_days": 30},
        )
        == "reserved"
    )


@pytest.mark.asyncio
async def test_manual_resolution_rejects_non_inconclusive_or_unsupported_action(
    database: Database,
) -> None:
    async with database.session() as session:
        action = OperatorAction(
            ticket_id="ticket-1",
            operator_telegram_id=42,
            action="remnawave_future_action",
            idempotency_key="unsupported-inconclusive",
            payload={"automatic_reconcile": "inconclusive"},
            result="unknown",
        )
        session.add(action)
        await session.commit()

    service = PanelService(FakeRemnawave(remnawave_user()), database=database)
    assert not await service.resolve_inconclusive_action(
        ticket_id="ticket-1",
        operator_action_id=action.id,
        operator_telegram_id=7,
        resolution="applied",
        idempotency_key="reject-unsupported",
    )
    async with database.session() as session:
        action = await session.get(OperatorAction, action.id)
        assert action is not None
        action.action = "remnawave_extend_subscription"
        action.payload = {"automatic_reconcile": "queued"}
        await session.commit()
    assert not await service.resolve_inconclusive_action(
        ticket_id="ticket-1",
        operator_action_id=action.id,
        operator_telegram_id=7,
        resolution="not_applied",
        idempotency_key="reject-pending",
    )
    with pytest.raises(ValueError, match="resolution"):
        await service.resolve_inconclusive_action(
            ticket_id="ticket-1",
            operator_action_id=action.id,
            operator_telegram_id=7,
            resolution="maybe",
            idempotency_key="reject-invalid-resolution",
        )


@pytest.mark.asyncio
async def test_exhausted_remnawave_read_becomes_notified_inconclusive(
    database: Database,
) -> None:
    action = await add_panel_action(
        database,
        key="exhausted-read",
        payload={"automatic_reconcile": "queued", "reconciliation_pending": True},
    )
    reconciliation_payload: dict[str, object] = {
        "identity_value": "123456789",
        "action": "extend_subscription",
        "before_expire_at": remnawave_user().expire_at.isoformat(),
        "request_payload": {"extend_days": 30},
    }
    async with database.session() as session:
        session.add(
            ReconciliationOutbox(
                idempotency_key="exhausted-read:reconciliation",
                kind="remnawave",
                ticket_id="ticket-1",
                operator_action_id=action.id,
                payload=reconciliation_payload,
                status=WorkStatus.PENDING,
                attempt_count=MAX_RECONCILIATION_ATTEMPTS - 1,
                next_attempt_at=utcnow(),
            )
        )
        await session.commit()

    remnawave = FakeRemnawave(RemnawaveUnavailableError("temporarily unavailable"))
    service = PanelService(remnawave, database=database, support_group_id=-100123)
    repository = DurableWorkRepository(database)
    job = await repository.claim_reconciliation()
    assert job is not None
    assert job.attempt_count == MAX_RECONCILIATION_ATTEMPTS

    async def unexpected_topic_reconciliation(ticket_id: str) -> bool:
        raise AssertionError(f"unexpected topic reconciliation for {ticket_id}")

    worker = ReconciliationWorker(
        repository=repository,
        reconcile_topic=unexpected_topic_reconciliation,
        panel_service=service,
    )
    await worker._process(job)

    async with database.session() as session:
        resolved = await session.get(OperatorAction, action.id)
        stored_job = await session.get(ReconciliationOutbox, job.id)
        notifications = list(
            (
                await session.scalars(
                    select(DeliveryOutbox).where(
                        DeliveryOutbox.idempotency_key
                        == f"panel-reconcile:{action.id}:operator-notification"
                    )
                )
            ).all()
        )
    assert resolved is not None
    assert resolved.result == "unknown"
    assert resolved.payload["automatic_reconcile"] == "inconclusive"
    assert resolved.payload["reconciliation_failure"] == "attempts_exhausted"
    assert resolved.payload["reconciliation_error_type"] == "RemnawaveUnavailableError"
    assert stored_job is not None
    assert stored_job.status == WorkStatus.DELIVERED
    assert len(notifications) == 1
    assert action.id in str(notifications[0].payload["text"])
    assert remnawave.telegram_ids == [123456789]

    assert await service.reconcile_durable_action(
        action.id,
        reconciliation_payload,
        attempt_count=MAX_RECONCILIATION_ATTEMPTS,
    )
    assert remnawave.telegram_ids == [123456789]


@pytest.mark.asyncio
async def test_invalid_durable_payload_becomes_inconclusive_without_retry(
    database: Database,
) -> None:
    action = await add_panel_action(
        database,
        key="invalid-durable-payload",
        payload={"automatic_reconcile": "queued", "reconciliation_pending": True},
    )
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, support_group_id=-100123)

    assert await service.reconcile_durable_action(action.id, {"action": "reset_key"})

    async with database.session() as session:
        resolved = await session.get(OperatorAction, action.id)
    assert resolved is not None
    assert resolved.result == "unknown"
    assert resolved.payload["automatic_reconcile"] == "inconclusive"
    assert resolved.payload["reconciliation_failure"] == "invalid_payload"
    assert remnawave.telegram_ids == []


@pytest.mark.parametrize(
    ("resolution", "expected_action_result", "expected_intent_status"),
    [
        ("applied", "completed", NotificationStatus.PENDING),
        ("not_applied", "not_applied", NotificationStatus.CANCELLED),
    ],
)
@pytest.mark.asyncio
async def test_manual_revoke_resolution_preserves_notification_guarantee(
    database: Database,
    resolution: str,
    expected_action_result: str,
    expected_intent_status: NotificationStatus,
) -> None:
    action = await add_panel_action(
        database,
        action="remnawave_revoke_subscription_link",
        key=f"manual-revoke-{resolution}",
        payload={
            "automatic_reconcile": "inconclusive",
            "requires_reconcile": True,
            "identity_value": "123456789",
        },
    )
    async with database.session() as session:
        session.add(
            NotificationOutbox(
                ticket_id="ticket-1",
                operator_action_id=action.id,
                idempotency_key=f"manual-revoke-{resolution}:notification",
                event_type="subscription_link_reissued",
                destination="subscription_owner",
                recipient_identity_provider="telegram",
                recipient_identity_value="123456789",
                payload={"before_subscription_url": "https://sub.example/old"},
                status=NotificationStatus.AWAITING_PAYLOAD,
            )
        )
        await session.commit()

    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)
    assert await service.resolve_inconclusive_action(
        ticket_id="ticket-1",
        operator_action_id=action.id,
        operator_telegram_id=7,
        resolution=resolution,
        idempotency_key=f"resolve-revoke-{resolution}",
    )

    async with database.session() as session:
        resolved = await session.get(OperatorAction, action.id)
        intent = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.operator_action_id == action.id)
        )
    assert resolved is not None
    assert resolved.result == expected_action_result
    assert intent is not None
    assert intent.status == expected_intent_status
    assert remnawave.revoke_calls == []
    if resolution == "applied":
        assert intent.payload["subscription_url"] == remnawave_user().subscription_url
        assert remnawave.telegram_ids == [123456789]
    else:
        assert remnawave.telegram_ids == []


@pytest.mark.asyncio
async def test_concurrent_manual_resolutions_have_one_consistent_winner(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = await add_panel_action(
        database,
        key="concurrent-manual-resolution",
        payload={"automatic_reconcile": "inconclusive", "requires_reconcile": True},
    )
    service = PanelService(FakeRemnawave(remnawave_user()), database=database)
    original_scalar = AsyncSession.scalar
    both_selected = asyncio.Event()
    release = asyncio.Event()
    participants: set[asyncio.Task[bool]] = set()
    selected_count = 0

    async def barrier_scalar(
        self: AsyncSession, statement: object, *args: object, **kwargs: object
    ):
        nonlocal selected_count
        result = await original_scalar(self, statement, *args, **kwargs)
        current = asyncio.current_task()
        if (
            current in participants
            and isinstance(result, OperatorAction)
            and result.id == action.id
        ):
            selected_count += 1
            if selected_count == 2:
                both_selected.set()
            await release.wait()
        return result

    monkeypatch.setattr(AsyncSession, "scalar", barrier_scalar)
    applied = asyncio.create_task(
        service.resolve_inconclusive_action(
            ticket_id="ticket-1",
            operator_action_id=action.id,
            operator_telegram_id=7,
            resolution="applied",
            idempotency_key="concurrent-applied",
        )
    )
    not_applied = asyncio.create_task(
        service.resolve_inconclusive_action(
            ticket_id="ticket-1",
            operator_action_id=action.id,
            operator_telegram_id=8,
            resolution="not_applied",
            idempotency_key="concurrent-not-applied",
        )
    )
    participants.update((applied, not_applied))
    await asyncio.wait_for(both_selected.wait(), timeout=2)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(applied, not_applied), timeout=10)

    async with database.session() as session:
        resolved = await session.get(OperatorAction, action.id)
        audits = list(
            (
                await session.scalars(
                    select(OperatorAction).where(
                        OperatorAction.action == "resolve_remnawave_action"
                    )
                )
            ).all()
        )
    assert sorted(results) == [False, True]
    assert resolved is not None
    assert len(audits) == 1
    assert resolved.payload["manual_reconcile"] == audits[0].payload["resolution"]
    assert resolved.payload["manual_reconcile_command_key"] == audits[0].idempotency_key
    assert resolved.payload["manual_reconciled_by_telegram_id"] == audits[0].operator_telegram_id


@pytest.mark.asyncio
async def test_late_automatic_reconciliation_cannot_overwrite_manual_resolution(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = await add_panel_action(
        database,
        key="late-automatic-reconciliation",
        payload={"automatic_reconcile": "queued", "reconciliation_pending": True},
    )
    initial = remnawave_user()
    applied_user = replace(initial, expire_at=initial.expire_at + timedelta(days=30))
    service = PanelService(FakeRemnawave(applied_user), database=database)
    reconciliation_payload: dict[str, object] = {
        "identity_value": "123456789",
        "action": "extend_subscription",
        "before_expire_at": initial.expire_at.isoformat(),
        "request_payload": {"extend_days": 30},
    }
    original_execute = AsyncSession.execute
    automatic_ready = asyncio.Event()
    release_automatic = asyncio.Event()
    automatic_task: asyncio.Task[bool] | None = None

    async def pause_automatic_update(
        self: AsyncSession, statement: object, *args: object, **kwargs: object
    ):
        if (
            asyncio.current_task() is automatic_task
            and isinstance(statement, Update)
            and statement.table.name == "operator_actions"
        ):
            automatic_ready.set()
            await release_automatic.wait()
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", pause_automatic_update)
    automatic_task = asyncio.create_task(
        service.reconcile_durable_action(action.id, reconciliation_payload)
    )
    await asyncio.wait_for(automatic_ready.wait(), timeout=2)
    async with database.session() as session:
        current = await session.get(OperatorAction, action.id)
        assert current is not None
        current.payload = {
            **current.payload,
            "automatic_reconcile": "inconclusive",
            "requires_reconcile": True,
        }
        await session.commit()
    assert await service.resolve_inconclusive_action(
        ticket_id="ticket-1",
        operator_action_id=action.id,
        operator_telegram_id=7,
        resolution="not_applied",
        idempotency_key="late-automatic-manual-winner",
    )
    release_automatic.set()
    assert await asyncio.wait_for(automatic_task, timeout=2)

    async with database.session() as session:
        resolved = await session.get(OperatorAction, action.id)
    assert resolved is not None
    assert resolved.result == "not_applied"
    assert resolved.payload["manual_reconcile"] == "not_applied"
    assert resolved.payload["automatic_reconcile"] == "inconclusive"


@pytest.mark.asyncio
async def test_concurrent_gift_reservations_are_serialized(database: Database) -> None:
    service = PanelService(FakeRemnawave(remnawave_user()), database=database)

    results = await asyncio.gather(
        service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=42,
            action="extend_subscription",
            idempotency_key="concurrent-gift-1",
            payload={"extend_days": 30},
        ),
        service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=43,
            action="extend_subscription",
            idempotency_key="concurrent-gift-2",
            payload={"extend_days": 30},
        ),
    )

    assert sorted(results) == ["needs_reconcile", "reserved"]


@pytest.mark.asyncio
async def test_different_panel_mutations_for_one_ticket_are_serialized(
    database: Database,
) -> None:
    service = PanelService(FakeRemnawave(remnawave_user()), database=database)

    results = await asyncio.gather(
        service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=42,
            action="extend_subscription",
            idempotency_key="concurrent-gift",
            payload={"extend_days": 30},
        ),
        service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=43,
            action="revoke_subscription_link",
            idempotency_key="concurrent-revoke",
            payload={},
        ),
    )

    assert sorted(results) == ["needs_reconcile", "reserved"]


@pytest.mark.asyncio
async def test_operator_action_tracks_update_and_completion_times(database: Database) -> None:
    service = PanelService(FakeRemnawave(remnawave_user()), database=database)
    key = "timestamped-panel-action"

    reserved = await service._reserve_action(
        ticket=ticket_view(),
        operator_telegram_id=42,
        action="extend_subscription",
        idempotency_key=key,
        payload={"extend_days": 30},
    )
    assert reserved == "reserved"
    async with database.session() as session:
        started = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == key)
        )
        assert started is not None
        initial_updated_at = started.updated_at
        assert started.completed_at is None

    await service._finish_action(key, "completed", {})

    async with database.session() as session:
        completed = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == key)
        )
        assert completed is not None
        assert completed.updated_at >= initial_updated_at
        assert completed.completed_at is not None
        assert completed.completed_at >= initial_updated_at


@pytest.mark.asyncio
async def test_reset_key_unknown_outcome_is_confirmed_automatically(
    database: Database,
) -> None:
    remnawave = AppliedUnknownResetKeyRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.reset_key_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:8:/resetkey",
    )

    action, job = await load_action_and_job(database, "telegram:-100:8:/resetkey")

    assert result.status == "unknown"
    assert remnawave.revoke_calls == [("11111111-1111-1111-1111-111111111111", True)]
    assert job.payload["before_credential_fingerprint"] == "a" * 64
    assert await service.reconcile_durable_action(action.id, job.payload) is True
    async with database.session() as session:
        action = await session.get(OperatorAction, action.id)
    assert action is not None
    assert action.result == "completed"
    assert action.payload["automatic_reconcile"] == "applied"


@pytest.mark.asyncio
async def test_revoke_link_is_confirmed_by_durable_reconciliation(database: Database) -> None:
    remnawave = AppliedUnknownRevokeLinkRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.revoke_subscription_link_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:9:/revokelink",
    )
    action, job = await load_action_and_job(database, "telegram:-100:9:/revokelink")
    assert result.status == "unknown"
    assert await service.reconcile_durable_action(action.id, job.payload) is True
    async with database.session() as session:
        notification = await session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key
                == "telegram:-100:9:/revokelink:user-notification"
            )
        )
    assert notification is not None
    assert notification.status == NotificationStatus.PENDING
    assert notification.payload["subscription_url"] == "https://sub.example/new-link"


@pytest.mark.asyncio
async def test_revoke_intent_exists_before_external_mutation(database: Database) -> None:
    class IntentCheckingRemnawave(FakeRemnawave):
        async def revoke_user_subscription(
            self, *, user_uuid: str, revoke_only_passwords: bool
        ) -> RemnawaveUser:
            async with database.session() as session:
                intent = await session.scalar(
                    select(NotificationOutbox).where(
                        NotificationOutbox.idempotency_key
                        == "telegram:-100:90:/revokelink:user-notification"
                    )
                )
            assert intent is not None
            assert intent.status == NotificationStatus.AWAITING_PAYLOAD
            assert intent.payload["before_subscription_url"] == "https://sub.example/abc123"
            self.revoke_calls.append((user_uuid, revoke_only_passwords))
            assert isinstance(self.result, RemnawaveUser)
            return replace(self.result, subscription_url="https://sub.example/reissued")

    remnawave = IntentCheckingRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)

    result = await service.revoke_subscription_link_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:90:/revokelink",
    )

    assert result.completed is True
    async with database.session() as session:
        intent = await session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key
                == "telegram:-100:90:/revokelink:user-notification"
            )
        )
    assert intent is not None
    assert intent.payload["subscription_url"] == "https://sub.example/reissued"


@pytest.mark.parametrize(
    ("telegram_enabled", "expected_delivery"),
    [(True, True), (False, False)],
)
@pytest.mark.asyncio
async def test_revoke_link_telegram_delivery_mode(
    database: Database,
    telegram_enabled: bool,
    expected_delivery: bool,
) -> None:
    class ReissuedRemnawave(FakeRemnawave):
        async def revoke_user_subscription(
            self, *, user_uuid: str, revoke_only_passwords: bool
        ) -> RemnawaveUser:
            self.revoke_calls.append((user_uuid, revoke_only_passwords))
            assert isinstance(self.result, RemnawaveUser)
            return replace(
                self.result,
                subscription_url="https://sub.example/mode-test",
            )

    key = f"telegram:-100:{int(telegram_enabled)}:/revokelink-mode"
    remnawave = ReissuedRemnawave(remnawave_user())
    service = PanelService(
        remnawave,
        database=database,
        revoke_link_telegram_notification=telegram_enabled,
    )

    result = await service.revoke_subscription_link_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key=key,
    )

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == key)
        )
        assert action is not None
        intent = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.operator_action_id == action.id)
        )
        delivery = await session.scalar(
            select(DeliveryOutbox).where(
                DeliveryOutbox.idempotency_key
                == f"panel-revoke-link:{action.id}:telegram-notification"
            )
        )

    assert result.completed is True
    assert action is not None
    assert intent is not None
    assert intent.status == NotificationStatus.PENDING
    assert intent.payload["subscription_url"] == "https://sub.example/mode-test"
    assert (delivery is not None) is expected_delivery
    if delivery is not None:
        assert delivery.payload == {
            "kind": "send_text",
            "target_chat_id": 123456789,
            "text": (
                "🔐 <b>Ссылка подписки обновлена</b>\n\n"
                "Старая ссылка больше не работает.\n\n"
                "Новая ссылка:\n"
                "<code>https://sub.example/mode-test</code>"
            ),
            "parse_mode": "HTML",
        }


@pytest.mark.asyncio
async def test_restart_recovers_prepared_revoke_intent_without_repeating_mutation(
    database: Database,
) -> None:
    initial = remnawave_user()
    remnawave = FakeRemnawave(initial)
    service = PanelService(remnawave, database=database)
    action_key = "telegram:-100:91:/revokelink"

    assert (
        await service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=42,
            action="revoke_subscription_link",
            idempotency_key=action_key,
            payload={"identity_provider": "telegram", "identity_value": "123456789"},
        )
        == "reserved"
    )
    lookup = await service.get_subscription_for_ticket(ticket_view())
    assert lookup.subscription is not None
    assert await service._prepare_revoke_intent(action_key, lookup.subscription) is True
    payload = service._reconciliation_payload(
        action="revoke_subscription_link",
        request_payload={"revoke_only_passwords": False},
        before=lookup.subscription,
    )
    await service._queue_durable_reconciliation(
        ticket=ticket_view(),
        idempotency_key=action_key,
        payload=payload,
    )
    remnawave.result = replace(initial, subscription_url="https://sub.example/recovered")
    remnawave.telegram_ids.clear()

    recovered = await service.recover_interrupted_actions()

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == action_key)
        )
        intent = await session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key == f"{action_key}:user-notification"
            )
        )
    assert recovered == 1
    assert remnawave.revoke_calls == []
    assert action is not None
    assert action.result == "unknown"
    assert action.payload["requires_reconcile"] is True
    assert intent is not None
    assert intent.status == NotificationStatus.AWAITING_PAYLOAD
    assert remnawave.telegram_ids == []
    async with database.session() as session:
        job = await session.scalar(
            select(ReconciliationOutbox).where(ReconciliationOutbox.operator_action_id == action.id)
        )
    assert job is not None
    assert await service.reconcile_durable_action(action.id, job.payload) is True


@pytest.mark.asyncio
async def test_restart_cancels_revoke_intent_interrupted_before_mutation(
    database: Database,
) -> None:
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)
    action_key = "telegram:-100:92:/revokelink"

    assert (
        await service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=42,
            action="revoke_subscription_link",
            idempotency_key=action_key,
            payload={"identity_provider": "telegram", "identity_value": "123456789"},
        )
        == "reserved"
    )

    recovered = await service.recover_interrupted_actions()

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == action_key)
        )
        intent = await session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key == f"{action_key}:user-notification"
            )
        )
    assert recovered == 1
    assert remnawave.telegram_ids == []
    assert remnawave.revoke_calls == []
    assert action is not None
    assert action.result == "not_applied"
    assert intent is not None
    assert intent.status == NotificationStatus.CANCELLED


@pytest.mark.asyncio
async def test_restart_recovery_is_idempotent_before_revoke_mutation(
    database: Database,
) -> None:
    remnawave = FakeRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database)
    action_key = "telegram:-100:93:/revokelink"

    assert (
        await service._reserve_action(
            ticket=ticket_view(),
            operator_telegram_id=42,
            action="revoke_subscription_link",
            idempotency_key=action_key,
            payload={"identity_provider": "telegram", "identity_value": "123456789"},
        )
        == "reserved"
    )
    lookup = await service.get_subscription_for_ticket(ticket_view())
    assert lookup.subscription is not None
    assert await service._prepare_revoke_intent(action_key, lookup.subscription) is True

    assert await service.recover_interrupted_actions() == 1
    assert await service.recover_interrupted_actions() == 0

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == action_key)
        )
        intent = await session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key == f"{action_key}:user-notification"
            )
        )
    assert action is not None
    assert action.result == "not_applied"
    assert intent is not None
    assert intent.status == NotificationStatus.CANCELLED


@pytest.mark.asyncio
async def test_reset_devices_unknown_outcome_is_confirmed_automatically(
    database: Database,
) -> None:
    remnawave = AppliedUnknownResetDevicesRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.reset_devices_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:10:/resetdevices",
    )

    action, job = await load_action_and_job(database, "telegram:-100:10:/resetdevices")

    assert result.status == "unknown"
    assert remnawave.reset_device_calls == ["11111111-1111-1111-1111-111111111111"]
    assert remnawave.get_device_calls == []
    assert await service.reconcile_durable_action(action.id, job.payload) is True
    async with database.session() as session:
        action = await session.get(OperatorAction, action.id)
    assert action is not None
    assert action.result == "completed"
    assert action.payload["automatic_reconcile"] == "applied"
    assert remnawave.get_device_calls == [
        "11111111-1111-1111-1111-111111111111",
    ]


@pytest.mark.asyncio
async def test_reset_devices_stays_blocked_when_result_is_inconclusive(
    database: Database,
) -> None:
    remnawave = UnknownResetDevicesRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.reset_devices_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:11:/resetdevices",
    )
    action, job = await load_action_and_job(database, "telegram:-100:11:/resetdevices")
    assert await service.reconcile_durable_action(action.id, job.payload, attempt_count=3) is True
    async with database.session() as session:
        action = await session.get(OperatorAction, action.id)

    assert result.status == "unknown"
    assert action is not None
    assert action.result == "unknown"
    assert action.payload["automatic_reconcile"] == "inconclusive"


@pytest.mark.asyncio
async def test_action_interrupted_before_mutation_is_recovered_as_not_applied(
    database: Database,
) -> None:
    async with database.session() as session:
        session.add_all(
            [
                OperatorAction(
                    ticket_id="ticket-1",
                    operator_telegram_id=42,
                    action="remnawave_reset_key",
                    idempotency_key="interrupted-panel-action",
                    payload={"identity_value": "123456789"},
                    result="started",
                ),
                OperatorAction(
                    ticket_id="ticket-1",
                    operator_telegram_id=42,
                    action="add_internal_note",
                    idempotency_key="unrelated-started-action",
                    payload={},
                    result="started",
                ),
            ]
        )
        await session.commit()

    service = PanelService(FakeRemnawave(remnawave_user()), database=database)
    recovered = await service.recover_interrupted_actions()

    async with database.session() as session:
        panel_action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == "interrupted-panel-action"
            )
        )
        unrelated_action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == "unrelated-started-action"
            )
        )

    assert recovered == 1
    assert panel_action is not None
    assert panel_action.result == "not_applied"
    assert panel_action.payload["requires_reconcile"] is False
    assert panel_action.payload["recovery_reason"] == "interrupted_before_mutation"
    assert unrelated_action is not None
    assert unrelated_action.result == "started"
