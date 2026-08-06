from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import resolvate.migrations as migrations_module
from resolvate.database import Database
from resolvate.migrations import (
    build_alembic_config,
    resolve_migration_database_url,
    upgrade_database,
)
from resolvate.models import Direction
from resolvate.services import TicketService

EXPECTED_QUERY_INDEXES = {
    "ix_tickets_status_updated",
    "ix_tickets_status_last_activity",
    "ix_ticket_messages_ticket_created",
    "ix_ticket_messages_sensitive_created",
    "ix_delivery_outbox_claim",
    "ix_delivery_outbox_stale",
    "ix_delivery_outbox_ticket_direction_status",
    "ix_delivery_outbox_ticket_status_created",
    "ix_notification_outbox_claim",
    "ix_notification_outbox_stale",
    "ix_notification_outbox_ticket_status_created",
    "ix_inbound_updates_ordering",
    "ix_reconciliation_ticket_status_created",
    "ix_operator_actions_ticket_action_result",
    "ix_operator_actions_result_created",
    "uq_operator_actions_unresolved_ticket",
    "ix_quick_responses_state_deadline",
}


async def index_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: {
                    index["name"]
                    for table_name in inspect(sync).get_table_names()
                    for index in inspect(sync).get_indexes(table_name)
                }
            )
    finally:
        await engine.dispose()


async def downgrade_to_revision(database_url: str, revision: str) -> None:
    config = build_alembic_config(database_url)
    await asyncio.to_thread(command.downgrade, config, revision)


async def current_revision(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision is not None
            return str(revision)
    finally:
        await engine.dispose()


async def test_migration_service_requires_a_dedicated_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL"):
        await migrations_module.run()


async def test_migration_service_uses_only_migration_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "postgresql+asyncpg://migrator:migration-password-123@postgres/resolvate"
    monkeypatch.setenv("MIGRATION_DATABASE_URL", target)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://runtime:runtime-password-123@postgres/resolvate",
    )
    observed: list[str] = []

    async def fake_upgrade(database_url: str) -> None:
        observed.append(database_url)

    monkeypatch.setattr(migrations_module, "upgrade_database", fake_upgrade)

    await migrations_module.run()

    assert observed == [target]


async def test_explicit_upgrade_target_ignores_ambient_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit_path = tmp_path / "explicit-upgrade.db"
    ambient_path = tmp_path / "ambient-upgrade.db"
    explicit_url = f"sqlite+aiosqlite:///{explicit_path}"
    ambient_url = f"sqlite+aiosqlite:///{ambient_path}"
    monkeypatch.setenv("DATABASE_URL", ambient_url)

    await upgrade_database(explicit_url)

    assert await current_revision(explicit_url) == "0020_quick_response_soft_delete"
    assert not ambient_path.exists()


async def test_explicit_downgrade_target_ignores_ambient_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit_url = f"sqlite+aiosqlite:///{tmp_path}/explicit-downgrade.db"
    ambient_url = f"sqlite+aiosqlite:///{tmp_path}/ambient-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    await upgrade_database(explicit_url)
    await upgrade_database(ambient_url)
    monkeypatch.setenv("DATABASE_URL", ambient_url)

    await downgrade_to_revision(explicit_url, "0009_ticket_last_activity")

    assert await current_revision(explicit_url) == "0009_ticket_last_activity"
    assert await current_revision(ambient_url) == "0020_quick_response_soft_delete"

    await upgrade_database(explicit_url)

    assert await current_revision(explicit_url) == "0020_quick_response_soft_delete"
    assert await current_revision(ambient_url) == "0020_quick_response_soft_delete"


async def test_alembic_cli_still_uses_ambient_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_url = f"sqlite+aiosqlite:///{tmp_path}/ambient-cli.db"
    monkeypatch.setenv("DATABASE_URL", ambient_url)
    config = Config("alembic.ini")
    config.attributes["skip_logging_config"] = True

    await asyncio.to_thread(command.upgrade, config, "head")

    assert await current_revision(ambient_url) == "0020_quick_response_soft_delete"


def test_alembic_config_accepts_percent_encoded_credentials() -> None:
    config = build_alembic_config(
        "postgresql+asyncpg://support:p%40ss%25word@postgres:5432/support"
    )

    assert resolve_migration_database_url(config, {}) == (
        "postgresql+psycopg://support:p%40ss%25word@postgres:5432/support"
    )


def test_empty_explicit_migration_target_is_rejected() -> None:
    config = build_alembic_config("")

    with pytest.raises(ValueError, match="must not be empty"):
        resolve_migration_database_url(config, {})


async def test_upgrade_head_creates_query_indexes(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/support.db"
    await upgrade_database(database_url)

    assert EXPECTED_QUERY_INDEXES <= await index_names(database_url)

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("delivery_outbox")
                }
            )
            notification_columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("notification_outbox")
                }
            )
            action_columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("operator_actions")
                }
            )
            ticket_columns = await connection.run_sync(
                lambda sync: {column["name"] for column in inspect(sync).get_columns("tickets")}
            )
            message_columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("ticket_messages")
                }
            )
            inbound_columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("inbound_updates")
                }
            )
            quick_response_columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("quick_responses")
                }
            )
            table_names = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
    finally:
        await engine.dispose()
    assert "delivered_message_id" in columns
    assert "claim_token" in columns
    assert "operator_action_id" in notification_columns
    assert {"updated_at", "completed_at"} <= action_columns
    assert "close_cycle" in ticket_columns
    assert "last_activity_at" in ticket_columns
    assert "rating_cycle" in message_columns
    assert "sensitive" in message_columns
    assert "ordering_key" in inbound_columns
    assert {
        "text",
        "tags",
        "created_by_telegram_id",
        "source_chat_id",
        "source_message_id",
        "published_message_id",
        "publication_format_version",
        "state",
        "invalid_until",
        "warning_message_id",
        "deleted_by_telegram_id",
        "deleted_at",
    } <= quick_response_columns
    assert "quick_replies" not in table_names
    assert "quick_reply_groups" not in table_names


async def test_flat_quick_response_migration_preserves_existing_replies(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/legacy-quick-replies.db"
    config = build_alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "0015_quick_replies")

    engine = create_async_engine(database_url)
    created_at = "2026-08-04 12:00:00+00:00"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO quick_replies ("
                    "title, normalized_title, text, created_by_telegram_id, "
                    "source_chat_id, source_message_id, active, created_at, updated_at"
                    ") VALUES ("
                    ":title, :normalized_title, :text, :created_by_telegram_id, "
                    ":source_chat_id, :source_message_id, :active, :created_at, :updated_at"
                    ")"
                ),
                {
                    "title": "Старый ответ",
                    "normalized_title": "старый ответ",
                    "text": "Старый текст",
                    "created_by_telegram_id": 7,
                    "source_chat_id": -100123,
                    "source_message_id": 501,
                    "active": True,
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            migrated = (
                await connection.execute(
                    text(
                        "SELECT text, tags, state, published_message_id, "
                        "publication_format_version FROM quick_responses"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert migrated.text == "Старый текст"
    assert json.loads(migrated.tags) == []
    assert migrated.state == "valid"
    assert migrated.published_message_id is None
    assert migrated.publication_format_version == 0


async def test_canonical_response_migration_preserves_status_and_resets_pending_publication(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/canonical-quick-responses.db"
    config = build_alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "0018_quick_response_status")

    engine = create_async_engine(database_url)
    created_at = "2026-08-04 12:00:00+00:00"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO quick_responses ("
                    "id, text, tags, created_by_telegram_id, source_chat_id, "
                    "source_message_id, published_message_id, state, invalid_until, "
                    "status_message_id, created_at, updated_at"
                    ") VALUES ("
                    "1, :valid_text, :tags, 7, -100123, 401, 401, :valid_state, NULL, "
                    "901, :created_at, :created_at"
                    "), ("
                    "2, :pending_text, :tags, 7, -100123, 402, 402, "
                    ":pending_state, :invalid_until, 902, :created_at, :created_at"
                    ")"
                ),
                {
                    "valid_text": "Сохранённый ответ #VPN",
                    "pending_text": "Невалидный ответ #1 #2 #3 #4 #5",
                    "valid_state": "valid",
                    "pending_state": "pending_deletion",
                    "tags": json.dumps([]),
                    "invalid_until": "2026-08-04 12:05:00+00:00",
                    "created_at": created_at,
                },
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, published_message_id, publication_format_version, "
                            "warning_message_id FROM quick_responses ORDER BY id"
                        )
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()

    assert rows[0]["published_message_id"] == 401
    assert rows[0]["publication_format_version"] == 0
    assert rows[0]["warning_message_id"] == 901
    assert rows[1]["published_message_id"] is None
    assert rows[1]["publication_format_version"] == 0
    assert rows[1]["warning_message_id"] == 902


async def test_query_indexes_support_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/support.db"
    await upgrade_database(database_url)

    await downgrade_to_revision(database_url, "0003_notification_outbox")
    assert EXPECTED_QUERY_INDEXES.isdisjoint(await index_names(database_url))

    await upgrade_database(database_url)
    assert EXPECTED_QUERY_INDEXES <= await index_names(database_url)


async def test_claim_ownership_migration_requeues_processing_deliveries(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/processing-upgrade.db"
    await upgrade_database(database_url)
    database = Database(database_url)
    try:
        ticket_service = TicketService(database)
        ticket = await ticket_service.open_or_reopen(
            telegram_user_id=3001,
            display_name="Migration Claim",
            username=None,
        )
        assert await ticket_service.enqueue_text(
            ticket_id=ticket.id,
            direction=Direction.OPERATOR_TO_USER,
            text="survive upgrade",
            target_chat_id=ticket.telegram_user_id,
            idempotency_key="migration-processing-delivery",
        )
        claimed = await ticket_service.outbox.claim_due_deliveries()
        assert len(claimed) == 1
        delivery_id = claimed[0].id
    finally:
        await database.dispose()

    await downgrade_to_revision(database_url, "0009_ticket_last_activity")
    await upgrade_database(database_url)

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            migrated = (
                await connection.execute(
                    text(
                        "SELECT status, claimed_at, claim_token "
                        "FROM delivery_outbox WHERE id = :delivery_id"
                    ),
                    {"delivery_id": delivery_id},
                )
            ).one()
    finally:
        await engine.dispose()

    assert migrated.status == "pending"
    assert migrated.claimed_at is None
    assert migrated.claim_token is None
