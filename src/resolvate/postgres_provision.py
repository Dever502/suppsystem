from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Self

import psycopg
from psycopg import sql

ROLE_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
URL_SAFE_PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9._~-]+$")
MIN_POSTGRES_PASSWORD_LENGTH = 16


@dataclass(frozen=True)
class PostgresProvisioningSettings:
    host: str
    port: int
    database: str
    admin_user: str
    admin_password: str
    migration_role: str
    migration_password: str
    runtime_role: str
    runtime_password: str

    @classmethod
    def from_environment(cls) -> Self:
        def required(name: str) -> str:
            value = os.getenv(name, "")
            if not value:
                raise ValueError(f"{name} is required for PostgreSQL role provisioning")
            return value

        settings = cls(
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DB", "resolvate"),
            admin_user=os.getenv("POSTGRES_ADMIN_USER", "postgres"),
            admin_password=required("POSTGRES_ADMIN_PASSWORD"),
            migration_role=os.getenv("POSTGRES_MIGRATION_USER", "resolvate_migrator"),
            migration_password=required("POSTGRES_MIGRATION_PASSWORD"),
            runtime_role=os.getenv("POSTGRES_RUNTIME_USER", "resolvate_runtime"),
            runtime_password=required("POSTGRES_RUNTIME_PASSWORD"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        for variable_name, value in (
            ("POSTGRES_DB", self.database),
            ("POSTGRES_ADMIN_USER", self.admin_user),
            ("POSTGRES_MIGRATION_USER", self.migration_role),
            ("POSTGRES_RUNTIME_USER", self.runtime_role),
        ):
            if not ROLE_NAME_PATTERN.fullmatch(value):
                raise ValueError(
                    f"{variable_name} must contain a safe lowercase PostgreSQL identifier"
                )
        if len({self.admin_user, self.migration_role, self.runtime_role}) != 3:
            raise ValueError("PostgreSQL admin, migration and runtime roles must be distinct")
        for variable_name, value in (
            ("POSTGRES_ADMIN_PASSWORD", self.admin_password),
            ("POSTGRES_MIGRATION_PASSWORD", self.migration_password),
            ("POSTGRES_RUNTIME_PASSWORD", self.runtime_password),
        ):
            if value != value.strip() or len(value) < MIN_POSTGRES_PASSWORD_LENGTH:
                raise ValueError(
                    f"{variable_name} must contain at least "
                    f"{MIN_POSTGRES_PASSWORD_LENGTH} non-whitespace characters"
                )
        for variable_name, value in (
            ("POSTGRES_MIGRATION_PASSWORD", self.migration_password),
            ("POSTGRES_RUNTIME_PASSWORD", self.runtime_password),
        ):
            if not URL_SAFE_PASSWORD_PATTERN.fullmatch(value):
                raise ValueError(
                    f"{variable_name} must contain only URL-safe characters "
                    "because Compose embeds it in a database URL"
                )
        if not 1 <= self.port <= 65535:
            raise ValueError("POSTGRES_PORT must be between 1 and 65535")


async def _ensure_login_role(
    cursor: psycopg.AsyncCursor[tuple[object, ...]], *, role: str, password: str
) -> None:
    await cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if await cursor.fetchone() is None:
        await cursor.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    await cursor.execute(
        sql.SQL(
            "ALTER ROLE {} WITH LOGIN PASSWORD {} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(sql.Identifier(role), sql.Literal(password))
    )


async def provision_postgres_roles(settings: PostgresProvisioningSettings) -> None:
    """Create and reconcile least-privilege migration/runtime roles.

    The bootstrap administrator is used only by this one-shot operation. Existing public-schema
    relations are reassigned to the migration role so an upgrade from the legacy single-role
    deployment remains possible. Runtime receives only data access, never DDL ownership.
    """

    settings.validate()
    connection = await psycopg.AsyncConnection.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.database,
        user=settings.admin_user,
        password=settings.admin_password,
        autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            await _ensure_login_role(
                cursor,
                role=settings.migration_role,
                password=settings.migration_password,
            )
            await _ensure_login_role(
                cursor,
                role=settings.runtime_role,
                password=settings.runtime_password,
            )

            await cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(settings.database)
                )
            )
            await cursor.execute(
                sql.SQL("GRANT CONNECT, CREATE, TEMPORARY ON DATABASE {} TO {}").format(
                    sql.Identifier(settings.database),
                    sql.Identifier(settings.migration_role),
                )
            )
            await cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(settings.database),
                    sql.Identifier(settings.runtime_role),
                )
            )
            await cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            await cursor.execute(
                sql.SQL("ALTER SCHEMA public OWNER TO {}").format(
                    sql.Identifier(settings.migration_role)
                )
            )

            await cursor.execute(
                "SELECT c.relkind, n.nspname, c.relname "
                "FROM pg_class AS c "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'v', 'm')"
            )
            relations = await cursor.fetchall()
            relation_keywords = {
                "r": "TABLE",
                "p": "TABLE",
                "S": "SEQUENCE",
                "v": "VIEW",
                "m": "MATERIALIZED VIEW",
            }
            # PostgreSQL requires an owned sequence and its table to have the same owner.
            # Reassign tables first; their owned sequences then follow automatically.
            for relation_kind, schema_name, relation_name in sorted(
                relations,
                key=lambda relation: str(relation[0]) == "S",
            ):
                keyword = relation_keywords[str(relation_kind)]
                await cursor.execute(
                    sql.SQL("ALTER {} {}.{} OWNER TO {}").format(
                        sql.SQL(keyword),
                        sql.Identifier(str(schema_name)),
                        sql.Identifier(str(relation_name)),
                        sql.Identifier(settings.migration_role),
                    )
                )

            await cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(settings.runtime_role)
                )
            )
            await cursor.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(
                    sql.Identifier(settings.runtime_role)
                )
            )
            await cursor.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                ).format(sql.Identifier(settings.runtime_role))
            )
            await cursor.execute(
                sql.SQL(
                    "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
                ).format(sql.Identifier(settings.runtime_role))
            )
            await cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(
                    sql.Identifier(settings.migration_role),
                    sql.Identifier(settings.runtime_role),
                )
            )
            await cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
                ).format(
                    sql.Identifier(settings.migration_role),
                    sql.Identifier(settings.runtime_role),
                )
            )
    finally:
        await connection.close()


async def run() -> None:
    await provision_postgres_roles(PostgresProvisioningSettings.from_environment())
    print("PostgreSQL migration and runtime roles are provisioned")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
