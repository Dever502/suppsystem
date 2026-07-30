from __future__ import annotations

import uuid

import pytest
from postgres_support import configured_postgres_admin_url
from sqlalchemy import make_url, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from suppsystem.database import Database
from suppsystem.migrations import upgrade_database
from suppsystem.postgres_provision import (
    PostgresProvisioningSettings,
    provision_postgres_roles,
)
from suppsystem.services import TicketService


def provisioning_settings(**overrides: object) -> PostgresProvisioningSettings:
    values: dict[str, object] = {
        "host": "postgres",
        "port": 5432,
        "database": "suppsystem",
        "admin_user": "postgres",
        "admin_password": "admin-password-123",
        "migration_role": "suppsystem_migrator",
        "migration_password": "migration-password-123",
        "runtime_role": "suppsystem_runtime",
        "runtime_password": "runtime-password-123",
    }
    values.update(overrides)
    return PostgresProvisioningSettings(**values)  # type: ignore[arg-type]


def test_postgres_provisioning_rejects_overlapping_roles() -> None:
    with pytest.raises(ValueError, match="must be distinct"):
        provisioning_settings(runtime_role="postgres").validate()


@pytest.mark.parametrize("role", ["Bad-Role", "role with space", "9role"])
def test_postgres_provisioning_rejects_unsafe_role_names(role: str) -> None:
    with pytest.raises(ValueError, match="safe lowercase"):
        provisioning_settings(runtime_role=role).validate()


def test_postgres_provisioning_rejects_non_url_safe_runtime_password() -> None:
    with pytest.raises(ValueError, match="URL-safe"):
        provisioning_settings(runtime_password="runtime:password@123").validate()


@pytest.mark.postgres
async def test_postgres_migration_and_runtime_roles_have_least_privilege() -> None:
    admin_url = make_url(configured_postgres_admin_url())
    suffix = uuid.uuid4().hex[:12]
    database_name = f"sbroles_{suffix}"
    migration_role = f"sbm_{suffix}"
    runtime_role = f"sbr_{suffix}"
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    database_created = False
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database_created = True

        child_admin_url = admin_url.set(database=database_name)
        await upgrade_database(child_admin_url.render_as_string(hide_password=False))

        settings = PostgresProvisioningSettings(
            host=str(admin_url.host),
            port=admin_url.port or 5432,
            database=database_name,
            admin_user=str(admin_url.username),
            admin_password=str(admin_url.password),
            migration_role=migration_role,
            migration_password="migration-password-123",
            runtime_role=runtime_role,
            runtime_password="runtime-password-123",
        )
        await provision_postgres_roles(settings)
        await provision_postgres_roles(settings)

        migration_url = child_admin_url.set(
            username=migration_role,
            password=settings.migration_password,
        ).render_as_string(hide_password=False)
        runtime_url = child_admin_url.set(
            username=runtime_role,
            password=settings.runtime_password,
        ).render_as_string(hide_password=False)
        await upgrade_database(migration_url)

        migration_engine = create_async_engine(migration_url)
        try:
            async with migration_engine.connect() as connection:
                table_owners = set(
                    (
                        await connection.execute(
                            text("SELECT tableowner FROM pg_tables WHERE schemaname = 'public'")
                        )
                    ).scalars()
                )
            assert table_owners == {migration_role}
        finally:
            await migration_engine.dispose()

        database = Database(runtime_url)
        try:
            service = TicketService(database)
            ticket = await service.open_or_reopen(
                telegram_user_id=123456789,
                display_name="Least Privilege",
                username="least_privilege",
            )
            assert ticket.telegram_user_id == 123456789

            async with database.engine.connect() as connection:
                current_user, can_create = (
                    await connection.execute(
                        text(
                            "SELECT current_user, "
                            "has_schema_privilege(current_user, 'public', 'CREATE')"
                        )
                    )
                ).one()
                role_flags = (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
                assert current_user == runtime_role
                assert can_create is False
                assert role_flags == (False, False, False, False, False)
                with pytest.raises(ProgrammingError):
                    await connection.execute(text("CREATE TABLE forbidden_runtime_ddl (id int)"))
        finally:
            await database.dispose()
    finally:
        if database_created:
            async with admin_engine.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
                await connection.execute(text(f'DROP ROLE IF EXISTS "{runtime_role}"'))
                await connection.execute(text(f'DROP ROLE IF EXISTS "{migration_role}"'))
        await admin_engine.dispose()
