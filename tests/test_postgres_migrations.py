from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from supportbot.migrations import build_alembic_config, upgrade_database
from supportbot.models import (
    Base,
    BlocklistEntry,
    DeliveryOutbox,
    NotificationOutbox,
    OperatorAction,
    Ticket,
    TicketMessage,
    User,
    UserIdentity,
)

pytestmark = pytest.mark.postgres

HEAD_REVISION = "0011_durable_ingress_reconciliation"
LEGACY_ACTIVE_TICKET_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_IDLE_TICKET_ID = "00000000-0000-4000-8000-000000000002"
LEGACY_MESSAGE_ID = "00000000-0000-4000-8000-000000000101"
LEGACY_PROCESSING_DELIVERY_ID = "00000000-0000-4000-8000-000000000201"
LEGACY_PENDING_DELIVERY_ID = "00000000-0000-4000-8000-000000000202"
LEGACY_TERMINAL_ACTION_ID = "00000000-0000-4000-8000-000000000301"
LEGACY_STARTED_ACTION_ID = "00000000-0000-4000-8000-000000000302"
LEGACY_UNFINISHED_ACTION_ID = "00000000-0000-4000-8000-000000000303"
LEGACY_NOTIFICATION_ID = "00000000-0000-4000-8000-000000000401"

CREATED_AT = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)
ACTIVE_UPDATED_AT = datetime(2025, 1, 2, 10, 0, tzinfo=UTC)
MESSAGE_CREATED_AT = datetime(2025, 1, 3, 10, 0, tzinfo=UTC)
IDLE_UPDATED_AT = datetime(2025, 1, 4, 10, 0, tzinfo=UTC)
ACTION_CREATED_AT = datetime(2025, 1, 5, 10, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ColumnContract:
    type_signature: tuple[str, int | None]
    nullable: bool | None


@dataclass(frozen=True)
class ForeignKeyContract:
    column_pairs: tuple[tuple[str, str], ...]
    referred_table: str
    ondelete: str | None


@dataclass(frozen=True)
class IndexContract:
    name: str
    columns: tuple[str, ...]
    unique: bool


@dataclass(frozen=True)
class TableContract:
    columns: dict[str, ColumnContract]
    primary_key: tuple[str, ...]
    unique_columns: frozenset[frozenset[str]]
    foreign_keys: frozenset[ForeignKeyContract]
    indexes: frozenset[IndexContract]


def _normalized_ondelete(value: object) -> str | None:
    if value is None:
        return None
    return str(value).upper()


def _type_signature(column_type: object) -> tuple[str, int | None]:
    if isinstance(column_type, sa.BigInteger):
        return ("bigint", None)
    if isinstance(column_type, sa.Integer):
        return ("integer", None)
    if isinstance(column_type, sa.Text):
        return ("text", None)
    if isinstance(column_type, sa.String):
        return ("string", column_type.length)
    if isinstance(column_type, sa.DateTime):
        return ("datetime", None)
    if isinstance(column_type, sa.JSON):
        return ("json", None)
    raise AssertionError(f"Unsupported schema type: {column_type!r}")


def _column_names(expressions: list[sa.ColumnElement[object]]) -> tuple[str, ...]:
    names: list[str] = []
    for expression in expressions:
        name = getattr(expression, "name", None)
        assert isinstance(name, str)
        names.append(name)
    return tuple(names)


def _metadata_schema_contract() -> dict[str, TableContract]:
    contract: dict[str, TableContract] = {}
    for table_name, table in Base.metadata.tables.items():
        primary_key = tuple(column.name for column in table.primary_key.columns)
        columns = {
            column.name: ColumnContract(
                type_signature=_type_signature(column.type),
                # Dialects disagree about reflected PK nullability even though
                # the primary-key constraint itself is authoritative.
                nullable=None if column.name in primary_key else column.nullable,
            )
            for column in table.columns
        }
        unique_columns = frozenset(
            frozenset(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        )
        foreign_keys: set[ForeignKeyContract] = set()
        for constraint in table.constraints:
            if not isinstance(constraint, sa.ForeignKeyConstraint):
                continue
            ondelete_values = {
                _normalized_ondelete(element.ondelete) for element in constraint.elements
            }
            assert len(ondelete_values) == 1
            foreign_keys.add(
                ForeignKeyContract(
                    column_pairs=tuple(
                        (element.parent.name, element.column.name)
                        for element in constraint.elements
                    ),
                    referred_table=constraint.elements[0].column.table.name,
                    ondelete=ondelete_values.pop(),
                )
            )
        indexes = frozenset(
            IndexContract(
                name=index.name,
                columns=_column_names(list(index.expressions)),
                unique=index.unique,
            )
            for index in table.indexes
            if index.name is not None
        )
        contract[table_name] = TableContract(
            columns=columns,
            primary_key=primary_key,
            unique_columns=unique_columns,
            foreign_keys=frozenset(foreign_keys),
            indexes=indexes,
        )
    return contract


def _database_schema_contract(
    connection: Connection, expected: dict[str, TableContract]
) -> dict[str, TableContract]:
    inspector = sa.inspect(connection)
    table_names = set(inspector.get_table_names())
    assert table_names == set(expected) | {"alembic_version"}

    contract: dict[str, TableContract] = {}
    for table_name, expected_table in expected.items():
        primary_key = tuple(
            str(name) for name in inspector.get_pk_constraint(table_name)["constrained_columns"]
        )
        columns = {
            str(column["name"]): ColumnContract(
                type_signature=_type_signature(column["type"]),
                nullable=None if str(column["name"]) in primary_key else bool(column["nullable"]),
            )
            for column in inspector.get_columns(table_name)
        }
        unique_columns = frozenset(
            frozenset(str(name) for name in constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table_name)
        )
        foreign_keys = frozenset(
            ForeignKeyContract(
                column_pairs=tuple(
                    zip(
                        (str(name) for name in foreign_key["constrained_columns"]),
                        (str(name) for name in foreign_key["referred_columns"]),
                        strict=True,
                    )
                ),
                referred_table=str(foreign_key["referred_table"]),
                ondelete=_normalized_ondelete((foreign_key.get("options") or {}).get("ondelete")),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        )
        indexes: set[IndexContract] = set()
        for index in inspector.get_indexes(table_name):
            # PostgreSQL reflects the backing indexes of UNIQUE constraints as
            # indexes too. The constraint is already compared above.
            if index.get("duplicates_constraint") is not None:
                continue
            name = index.get("name")
            assert isinstance(name, str)
            column_names = index.get("column_names")
            assert isinstance(column_names, list)
            assert all(isinstance(column_name, str) for column_name in column_names)
            indexes.add(
                IndexContract(
                    name=name,
                    columns=tuple(column_names),
                    unique=bool(index.get("unique", False)),
                )
            )
        contract[table_name] = TableContract(
            columns=columns,
            primary_key=primary_key,
            unique_columns=unique_columns,
            foreign_keys=foreign_keys,
            indexes=frozenset(indexes),
        )
        assert contract[table_name] == expected_table
    return contract


async def _upgrade_to_revision(database_url: str, revision: str) -> None:
    config = build_alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, revision)


async def _current_revision(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            assert revision is not None
            return str(revision)
    finally:
        await engine.dispose()


def _seed_revision_0001(connection: Connection) -> None:
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=connection)
    identities = sa.Table("user_identities", metadata, autoload_with=connection)
    tickets = sa.Table("tickets", metadata, autoload_with=connection)
    messages = sa.Table("ticket_messages", metadata, autoload_with=connection)
    deliveries = sa.Table("delivery_outbox", metadata, autoload_with=connection)
    actions = sa.Table("operator_actions", metadata, autoload_with=connection)
    blocklist = sa.Table("blocklist", metadata, autoload_with=connection)

    connection.execute(
        users.insert(),
        [
            {
                "id": 1,
                "display_name": "Legacy Active",
                "username": "legacy_active",
                "created_at": CREATED_AT,
                "updated_at": ACTIVE_UPDATED_AT,
            },
            {
                "id": 2,
                "display_name": "Legacy Idle",
                "username": None,
                "created_at": CREATED_AT,
                "updated_at": IDLE_UPDATED_AT,
            },
        ],
    )
    connection.execute(
        identities.insert(),
        {
            "id": 1,
            "user_id": 1,
            "provider": "telegram",
            "external_id": "71001",
            "created_at": CREATED_AT,
        },
    )
    connection.execute(
        tickets.insert(),
        [
            {
                "id": LEGACY_ACTIVE_TICKET_ID,
                "user_id": 1,
                "topic_id": 91001,
                "status": "open",
                "topic_provisioning_token": None,
                "topic_provisioning_started_at": None,
                "created_at": CREATED_AT,
                "updated_at": ACTIVE_UPDATED_AT,
                "closed_at": None,
            },
            {
                "id": LEGACY_IDLE_TICKET_ID,
                "user_id": 2,
                "topic_id": None,
                "status": "closed",
                "topic_provisioning_token": None,
                "topic_provisioning_started_at": None,
                "created_at": CREATED_AT,
                "updated_at": IDLE_UPDATED_AT,
                "closed_at": IDLE_UPDATED_AT,
            },
        ],
    )
    connection.execute(
        messages.insert(),
        {
            "id": LEGACY_MESSAGE_ID,
            "ticket_id": LEGACY_ACTIVE_TICKET_ID,
            "direction": "user_to_operator",
            "channel": "telegram",
            "source_chat_id": 71001,
            "source_message_id": 17,
            "created_at": MESSAGE_CREATED_AT,
        },
    )
    connection.execute(
        deliveries.insert(),
        [
            {
                "id": LEGACY_PROCESSING_DELIVERY_ID,
                "ticket_id": LEGACY_ACTIVE_TICKET_ID,
                "idempotency_key": "legacy-processing-delivery",
                "direction": "operator_to_user",
                "payload": {
                    "kind": "send_text",
                    "target_chat_id": 71001,
                    "text": "legacy processing",
                },
                "status": "processing",
                "attempt_count": 1,
                "next_attempt_at": MESSAGE_CREATED_AT,
                "claimed_at": MESSAGE_CREATED_AT,
                "delivered_at": None,
                "last_error": None,
                "created_at": MESSAGE_CREATED_AT,
            },
            {
                "id": LEGACY_PENDING_DELIVERY_ID,
                "ticket_id": LEGACY_IDLE_TICKET_ID,
                "idempotency_key": "legacy-pending-delivery",
                "direction": "operator_to_user",
                "payload": {
                    "kind": "send_text",
                    "target_chat_id": 71002,
                    "text": "legacy pending",
                },
                "status": "pending",
                "attempt_count": 0,
                "next_attempt_at": IDLE_UPDATED_AT,
                "claimed_at": None,
                "delivered_at": None,
                "last_error": None,
                "created_at": IDLE_UPDATED_AT,
            },
        ],
    )
    connection.execute(
        actions.insert(),
        [
            {
                "id": LEGACY_TERMINAL_ACTION_ID,
                "ticket_id": LEGACY_ACTIVE_TICKET_ID,
                "operator_telegram_id": 42,
                "action": "close_ticket",
                "idempotency_key": "legacy-terminal-action",
                "payload": {},
                "result": "completed",
                "trace_id": "legacy-terminal-trace",
                "created_at": ACTION_CREATED_AT,
            },
            {
                "id": LEGACY_STARTED_ACTION_ID,
                "ticket_id": LEGACY_IDLE_TICKET_ID,
                "operator_telegram_id": 43,
                "action": "panel_action",
                "idempotency_key": "legacy-started-action",
                "payload": {},
                "result": "started",
                "trace_id": None,
                "created_at": ACTION_CREATED_AT,
            },
            {
                "id": LEGACY_UNFINISHED_ACTION_ID,
                "ticket_id": LEGACY_IDLE_TICKET_ID,
                "operator_telegram_id": 44,
                "action": "internal_note",
                "idempotency_key": "legacy-unfinished-action",
                "payload": {},
                "result": None,
                "trace_id": None,
                "created_at": ACTION_CREATED_AT,
            },
        ],
    )
    connection.execute(
        blocklist.insert(),
        {
            "telegram_user_id": 71999,
            "blocked_by_telegram_id": 42,
            "reason": "legacy spam",
            "created_at": CREATED_AT,
        },
    )


def _seed_revision_0003(connection: Connection) -> None:
    metadata = sa.MetaData()
    notifications = sa.Table("notification_outbox", metadata, autoload_with=connection)
    connection.execute(
        notifications.insert(),
        {
            "id": LEGACY_NOTIFICATION_ID,
            "ticket_id": LEGACY_ACTIVE_TICKET_ID,
            "idempotency_key": "legacy-notification",
            "event_type": "ticket_closed",
            "destination": "webhook",
            "recipient_identity_provider": "telegram",
            "recipient_identity_value": "71001",
            "payload": {"text": "legacy notification"},
            "status": "pending",
            "attempt_count": 0,
            "next_attempt_at": ACTION_CREATED_AT,
            "claimed_at": None,
            "delivered_at": None,
            "last_error": None,
            "created_at": ACTION_CREATED_AT,
        },
    )


async def _seed(database_url: str, seed_function: object) -> None:
    assert callable(seed_function)
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(seed_function)
    finally:
        await engine.dispose()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _assert_fresh_upgrade_matches_semantic_orm_schema(database_url: str) -> None:
    await upgrade_database(database_url)

    expected = _metadata_schema_contract()
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            actual = await connection.run_sync(
                lambda sync_connection: _database_schema_contract(sync_connection, expected)
            )
    finally:
        await engine.dispose()

    assert await _current_revision(database_url) == HEAD_REVISION
    assert actual == expected


async def _assert_0001_to_head_preserves_and_backfills_legacy_data(
    database_url: str,
) -> None:
    await _upgrade_to_revision(database_url, "0001_initial_schema")
    await _seed(database_url, _seed_revision_0001)
    await _upgrade_to_revision(database_url, "0003_notification_outbox")
    await _seed(database_url, _seed_revision_0003)
    await upgrade_database(database_url)

    expected = _metadata_schema_contract()
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            actual = await connection.run_sync(
                lambda sync_connection: _database_schema_contract(sync_connection, expected)
            )
            counts = {
                "users": await connection.scalar(sa.select(sa.func.count()).select_from(User)),
                "identities": await connection.scalar(
                    sa.select(sa.func.count()).select_from(UserIdentity)
                ),
                "tickets": await connection.scalar(sa.select(sa.func.count()).select_from(Ticket)),
                "messages": await connection.scalar(
                    sa.select(sa.func.count()).select_from(TicketMessage)
                ),
                "deliveries": await connection.scalar(
                    sa.select(sa.func.count()).select_from(DeliveryOutbox)
                ),
                "actions": await connection.scalar(
                    sa.select(sa.func.count()).select_from(OperatorAction)
                ),
                "notifications": await connection.scalar(
                    sa.select(sa.func.count()).select_from(NotificationOutbox)
                ),
                "blocklist": await connection.scalar(
                    sa.select(sa.func.count()).select_from(BlocklistEntry)
                ),
            }
            active_ticket = (
                await connection.execute(
                    sa.select(
                        Ticket.status,
                        Ticket.close_cycle,
                        Ticket.last_activity_at,
                    ).where(Ticket.id == LEGACY_ACTIVE_TICKET_ID)
                )
            ).one()
            idle_ticket = (
                await connection.execute(
                    sa.select(
                        Ticket.status,
                        Ticket.close_cycle,
                        Ticket.last_activity_at,
                        Ticket.closed_at,
                    ).where(Ticket.id == LEGACY_IDLE_TICKET_ID)
                )
            ).one()
            message = (
                await connection.execute(
                    sa.select(
                        TicketMessage.content,
                        TicketMessage.media,
                        TicketMessage.rating_cycle,
                        TicketMessage.source_chat_id,
                        TicketMessage.source_message_id,
                    ).where(TicketMessage.id == LEGACY_MESSAGE_ID)
                )
            ).one()
            processing_delivery = (
                await connection.execute(
                    sa.select(
                        DeliveryOutbox.status,
                        DeliveryOutbox.attempt_count,
                        DeliveryOutbox.claimed_at,
                        DeliveryOutbox.claim_token,
                        DeliveryOutbox.delivered_message_id,
                        DeliveryOutbox.payload,
                    ).where(DeliveryOutbox.id == LEGACY_PROCESSING_DELIVERY_ID)
                )
            ).one()
            pending_delivery = (
                await connection.execute(
                    sa.select(
                        DeliveryOutbox.status,
                        DeliveryOutbox.attempt_count,
                        DeliveryOutbox.claimed_at,
                        DeliveryOutbox.claim_token,
                    ).where(DeliveryOutbox.id == LEGACY_PENDING_DELIVERY_ID)
                )
            ).one()
            actions = {
                row.id: row
                for row in (
                    await connection.execute(
                        sa.select(
                            OperatorAction.id,
                            OperatorAction.created_at,
                            OperatorAction.updated_at,
                            OperatorAction.completed_at,
                        ).where(
                            OperatorAction.id.in_(
                                {
                                    LEGACY_TERMINAL_ACTION_ID,
                                    LEGACY_STARTED_ACTION_ID,
                                    LEGACY_UNFINISHED_ACTION_ID,
                                }
                            )
                        )
                    )
                ).all()
            }
            notification = (
                await connection.execute(
                    sa.select(
                        NotificationOutbox.operator_action_id,
                        NotificationOutbox.payload,
                        NotificationOutbox.status,
                    ).where(NotificationOutbox.id == LEGACY_NOTIFICATION_ID)
                )
            ).one()
    finally:
        await engine.dispose()

    assert await _current_revision(database_url) == HEAD_REVISION
    assert actual == expected
    assert counts == {
        "users": 2,
        "identities": 1,
        "tickets": 2,
        "messages": 1,
        "deliveries": 2,
        "actions": 3,
        "notifications": 1,
        "blocklist": 1,
    }
    assert active_ticket.status == "open"
    assert active_ticket.close_cycle == 0
    assert _as_utc(active_ticket.last_activity_at) == MESSAGE_CREATED_AT
    assert idle_ticket.status == "closed"
    assert idle_ticket.close_cycle == 0
    assert _as_utc(idle_ticket.last_activity_at) == IDLE_UPDATED_AT
    assert _as_utc(idle_ticket.closed_at) == IDLE_UPDATED_AT
    assert message.content is None
    assert message.media is None
    assert message.rating_cycle is None
    assert message.source_chat_id == 71001
    assert message.source_message_id == 17
    assert processing_delivery.status == "pending"
    assert processing_delivery.attempt_count == 1
    assert processing_delivery.claimed_at is None
    assert processing_delivery.claim_token is None
    assert processing_delivery.delivered_message_id is None
    assert processing_delivery.payload == {
        "kind": "send_text",
        "target_chat_id": 71001,
        "text": "legacy processing",
    }
    assert pending_delivery.status == "pending"
    assert pending_delivery.attempt_count == 0
    assert pending_delivery.claimed_at is None
    assert pending_delivery.claim_token is None

    terminal_action = actions[LEGACY_TERMINAL_ACTION_ID]
    assert _as_utc(terminal_action.updated_at) == _as_utc(terminal_action.created_at)
    assert _as_utc(terminal_action.completed_at) == _as_utc(terminal_action.created_at)
    for action_id in (LEGACY_STARTED_ACTION_ID, LEGACY_UNFINISHED_ACTION_ID):
        action = actions[action_id]
        assert _as_utc(action.updated_at) == _as_utc(action.created_at)
        assert action.completed_at is None

    assert notification.operator_action_id is None
    assert notification.payload == {"text": "legacy notification"}
    assert notification.status == "pending"


async def test_sqlite_fresh_upgrade_matches_semantic_orm_schema(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/fresh.db"

    await _assert_fresh_upgrade_matches_semantic_orm_schema(database_url)


async def test_postgres_fresh_upgrade_matches_semantic_orm_schema(
    postgres_database_url: str,
) -> None:
    await _assert_fresh_upgrade_matches_semantic_orm_schema(postgres_database_url)


async def test_sqlite_0001_to_head_preserves_and_backfills_legacy_data(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/legacy.db"

    await _assert_0001_to_head_preserves_and_backfills_legacy_data(database_url)


async def test_postgres_0001_to_head_preserves_and_backfills_legacy_data(
    postgres_database_url: str,
) -> None:
    await _assert_0001_to_head_preserves_and_backfills_legacy_data(postgres_database_url)
