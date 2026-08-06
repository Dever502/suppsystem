from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from postgres_support import disposable_postgres_database

from resolvate.migrations import upgrade_database

SQLiteDatabaseURLFactory = Callable[[Path], str]


@pytest.fixture(scope="session")
def migrated_sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the current SQLite schema once per pytest worker."""

    template_path = tmp_path_factory.mktemp("migrated-sqlite") / "template.db"
    database_url = f"sqlite+aiosqlite:///{template_path}"
    asyncio.run(upgrade_database(database_url))
    template_path.chmod(0o444)
    return template_path


@pytest.fixture
def migrated_sqlite_database_url(
    migrated_sqlite_template: Path,
) -> SQLiteDatabaseURLFactory:
    """Clone the migrated template into an isolated writable test database."""

    def create_database_url(database_path: Path) -> str:
        if database_path.exists():
            raise FileExistsError(f"Refusing to replace test database: {database_path}")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(migrated_sqlite_template, database_path)
        return f"sqlite+aiosqlite:///{database_path}"

    return create_database_url


@pytest.fixture
async def postgres_database_url() -> AsyncIterator[str]:
    async for database_url in disposable_postgres_database():
        yield database_url
