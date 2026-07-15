from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from supportbot.database import Database
from supportbot.models import (
    NotificationOutbox,
    NotificationStatus,
    OperatorAction,
    ReconciliationOutbox,
    Ticket,
    TicketStatus,
    User,
)
from supportbot.panel import PanelService
from supportbot.remnawave import (
    RemnawaveAmbiguousIdentityError,
    RemnawaveBulkActionResult,
    RemnawaveHwidDeviceResetResult,
    RemnawaveNotFoundError,
    RemnawaveTraffic,
    RemnawaveUnavailableError,
    RemnawaveUnknownOutcomeError,
    RemnawaveUser,
)
from supportbot.services import TicketView
from supportbot.telegram_adapter import TelegramSupportAdapter


class FakeRemnawave:
    def __init__(self, result: RemnawaveUser | Exception) -> None:
        self.result = result
        self.telegram_ids: list[int] = []
        self.extend_calls: list[tuple[str, int]] = []
        self.revoke_calls: list[tuple[str, bool]] = []
        self.reset_device_calls: list[str] = []

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

    async def reset_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        self.reset_device_calls.append(user_uuid)
        return RemnawaveHwidDeviceResetResult(total=2, devices=[])


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


class RetryOnceResetKeyRemnawave(FakeRemnawave):
    async def revoke_user_subscription(
        self, *, user_uuid: str, revoke_only_passwords: bool
    ) -> RemnawaveUser:
        self.revoke_calls.append((user_uuid, revoke_only_passwords))
        if len(self.revoke_calls) == 1:
            raise RemnawaveUnknownOutcomeError("first reset response was lost")
        assert isinstance(self.result, RemnawaveUser)
        return self.result


class AppliedUnknownRevokeLinkRemnawave(FakeRemnawave):
    async def revoke_user_subscription(
        self, *, user_uuid: str, revoke_only_passwords: bool
    ) -> RemnawaveUser:
        self.revoke_calls.append((user_uuid, revoke_only_passwords))
        assert revoke_only_passwords is False
        assert isinstance(self.result, RemnawaveUser)
        self.result = replace(self.result, subscription_url="https://sub.example/new-link")
        raise RemnawaveUnknownOutcomeError("revoke response was lost")


class RetryOnceResetDevicesRemnawave(FakeRemnawave):
    async def reset_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        self.reset_device_calls.append(user_uuid)
        if len(self.reset_device_calls) == 1:
            raise RemnawaveUnknownOutcomeError("first reset response was lost")
        return RemnawaveHwidDeviceResetResult(total=0, devices=[])


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
        email="user@example.com",
        hwid_device_limit=5,
        traffic=RemnawaveTraffic(
            used_traffic_bytes=1024,
            lifetime_used_traffic_bytes=2048,
            online_at=None,
        ),
    )


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


@pytest.mark.asyncio
async def test_panel_service_maps_not_found() -> None:
    service = PanelService(FakeRemnawave(RemnawaveNotFoundError()))

    lookup = await service.get_subscription_for_ticket(ticket_view())

    assert lookup.status == "not_found"
    assert lookup.subscription is None


@pytest.mark.asyncio
async def test_panel_service_maps_ambiguous_identity() -> None:
    service = PanelService(FakeRemnawave(RemnawaveAmbiguousIdentityError()))

    lookup = await service.get_subscription_for_ticket(ticket_view())

    assert lookup.status == "ambiguous_identity"
    assert lookup.subscription is None


def test_telegram_adapter_explains_ambiguous_identity() -> None:
    from supportbot.telegram_adapter import TelegramSupportAdapter

    assert TelegramSupportAdapter._panel_status_text("ambiguous_identity") == (
        "найдено несколько пользователей с этим Telegram ID; операция заблокирована"
    )


@pytest.mark.asyncio
async def test_panel_service_maps_unavailable() -> None:
    service = PanelService(FakeRemnawave(RemnawaveUnavailableError()))

    lookup = await service.get_subscription_for_ticket(ticket_view())

    assert lookup.status == "unavailable"
    assert lookup.subscription is None


def test_telegram_adapter_formats_subscription_lookup() -> None:
    from supportbot.panel import PanelSubscriptionLookup, _subscription_info
    from supportbot.telegram_adapter import TOPIC_COMMANDS

    lookup = PanelSubscriptionLookup(
        status="found",
        identity_provider="telegram",
        identity_value="123456789",
        subscription=_subscription_info(remnawave_user()),
    )

    text = TelegramSupportAdapter._format_subscription_lookup(lookup)
    expiration = TelegramSupportAdapter._expiration_text(remnawave_user().expire_at)

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

    assert TelegramSupportAdapter._expiration_text(expire_at, now=now) == expected


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

    assert first.completed is True
    assert first.affected_rows == 1
    assert duplicate.status == "duplicate"
    assert remnawave.telegram_ids == [123456789]
    assert remnawave.extend_calls == [("11111111-1111-1111-1111-111111111111", 30)]
    assert action is not None
    assert action.action == "remnawave_extend_subscription"
    assert action.result == "completed"
    assert action.payload["extend_days"] == 30
    assert action.payload["remnawave_uuid"] == "11111111-1111-1111-1111-111111111111"
    assert "subscription_url" not in action.payload


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
    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == "telegram:-100:6:/gift")
        )
        job = await session.scalar(
            select(ReconciliationOutbox).where(ReconciliationOutbox.operator_action_id == action.id)
        )
    assert result.status == "unknown"
    assert action is not None and job is not None
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

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == "telegram:-100:6:/gift")
        )

    assert first.status == "unknown"
    assert first.changed is False
    assert repeat.status == "needs_reconcile"
    assert remnawave.extend_calls == [
        ("11111111-1111-1111-1111-111111111111", 30),
    ]
    assert action is not None
    assert action.result == "unknown"
    assert action.payload["automatic_reconcile"] == "queued"


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
    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == "telegram:-100:70:/gift")
        )
        job = await session.scalar(
            select(ReconciliationOutbox).where(ReconciliationOutbox.operator_action_id == action.id)
        )
    assert result.status == "unknown"
    assert action is not None and job is not None
    assert await service.reconcile_durable_action(action.id, job.payload) is False
    assert await service.reconcile_durable_action(action.id, job.payload) is True


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
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.extend_subscription_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        extend_days=30,
        idempotency_key="telegram:-100:71:/gift",
    )

    assert result.status == "unknown"
    assert result.changed is False


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
async def test_reset_key_unknown_outcome_requires_manual_reconciliation(
    database: Database,
) -> None:
    remnawave = RetryOnceResetKeyRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.reset_key_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:8:/resetkey",
    )

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == "telegram:-100:8:/resetkey"
            )
        )

    assert result.status == "unknown"
    assert remnawave.revoke_calls == [("11111111-1111-1111-1111-111111111111", True)]
    assert action is not None
    assert action.payload["automatic_reconcile"] == "manual_review_required"
    assert action.payload["requires_reconcile"] is True


@pytest.mark.asyncio
async def test_revoke_link_is_confirmed_by_durable_reconciliation(database: Database) -> None:
    remnawave = AppliedUnknownRevokeLinkRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.revoke_subscription_link_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:9:/revokelink",
    )
    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == "telegram:-100:9:/revokelink"
            )
        )
        job = await session.scalar(
            select(ReconciliationOutbox).where(ReconciliationOutbox.operator_action_id == action.id)
        )
    assert result.status == "unknown"
    assert action is not None and job is not None
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
async def test_restart_does_not_reprocess_ready_revoke_notification(
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
    assert action.result == "unknown"
    assert intent is not None
    assert intent.status == NotificationStatus.AWAITING_PAYLOAD


@pytest.mark.asyncio
async def test_reset_devices_unknown_outcome_requires_manual_reconciliation(
    database: Database,
) -> None:
    remnawave = RetryOnceResetDevicesRemnawave(remnawave_user())
    service = PanelService(remnawave, database=database, reconcile_delay_seconds=0)

    result = await service.reset_devices_for_ticket(
        ticket=ticket_view(),
        operator_telegram_id=42,
        idempotency_key="telegram:-100:10:/resetdevices",
    )

    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == "telegram:-100:10:/resetdevices"
            )
        )

    assert result.status == "unknown"
    assert remnawave.reset_device_calls == ["11111111-1111-1111-1111-111111111111"]
    assert action is not None
    assert action.payload["automatic_reconcile"] == "manual_review_required"
    assert action.payload["requires_reconcile"] is True


@pytest.mark.asyncio
async def test_interrupted_panel_actions_are_recovered_as_unknown(database: Database) -> None:
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
    assert panel_action.result == "unknown"
    assert panel_action.payload["requires_reconcile"] is True
    assert panel_action.payload["recovery_reason"] == "process_interrupted"
    assert unrelated_action is not None
    assert unrelated_action.result == "started"
