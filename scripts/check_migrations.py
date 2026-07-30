from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from suppsystem.migrations import upgrade_database
from suppsystem.models import Base


async def check_migrations() -> None:
    with TemporaryDirectory(prefix="suppsystem-migrations-") as directory:
        database_path = Path(directory) / "support.db"
        database_url = f"sqlite+aiosqlite:///{database_path}"
        await upgrade_database(database_url)

        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:

                def inspect_schema(sync_connection: object) -> tuple[list[str], set[str]]:
                    inspector = inspect(sync_connection)
                    table_names = inspector.get_table_names()
                    index_names = {
                        index["name"]
                        for table_name in table_names
                        for index in inspector.get_indexes(table_name)
                        if index["name"] is not None
                    }
                    return table_names, index_names

                table_names, actual_indexes = await connection.run_sync(inspect_schema)
                actual_tables = set(table_names)
        finally:
            await engine.dispose()

        expected_tables = set(Base.metadata.tables)
        missing_tables = expected_tables - actual_tables
        if missing_tables:
            names = ", ".join(sorted(missing_tables))
            raise RuntimeError(f"Alembic schema is missing ORM tables: {names}")

        expected_indexes = {
            index.name
            for table in Base.metadata.tables.values()
            for index in table.indexes
            if index.name is not None
        }
        missing_indexes = expected_indexes - actual_indexes
        if missing_indexes:
            names = ", ".join(sorted(missing_indexes))
            raise RuntimeError(f"Alembic schema is missing ORM indexes: {names}")


if __name__ == "__main__":
    asyncio.run(check_migrations())
