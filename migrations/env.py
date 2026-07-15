from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from supportbot.database import configure_sqlite_connection
from supportbot.migrations import resolve_migration_database_url
from supportbot.models import Base

config = context.config

if config.config_file_name is not None and not config.attributes.get("skip_logging_config", False):
    fileConfig(config.config_file_name)


database_url = resolve_migration_database_url(config)

target_metadata = Base.metadata


def _configure_sqlite_migration_connection(dbapi_connection: Any, connection_record: Any) -> None:
    configure_sqlite_connection(dbapi_connection, connection_record)
    cursor = dbapi_connection.cursor()
    try:
        # Alembic batch mode recreates parent tables on SQLite. Enforcing
        # ON DELETE while the old parent is dropped would cascade-delete
        # child rows even though the migration is only changing the schema.
        cursor.execute("PRAGMA foreign_keys=OFF")
    finally:
        cursor.close()


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine_configuration = config.get_section(config.config_ini_section, {}) or {}
    engine_configuration["sqlalchemy.url"] = database_url
    connectable = engine_from_config(
        engine_configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    is_sqlite = connectable.dialect.name == "sqlite"
    if is_sqlite:
        event.listen(connectable, "connect", _configure_sqlite_migration_connection)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
            if is_sqlite:
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if violations:
                    tables = sorted({str(row[0]) for row in violations})
                    raise RuntimeError(
                        "SQLite migration created foreign-key violations in: " + ", ".join(tables)
                    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
