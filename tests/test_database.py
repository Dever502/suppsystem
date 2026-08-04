from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from suppsystem.database import Database
from suppsystem.runtime_defaults import POSTGRES_POOL_SIZE


async def test_sqlite_connections_enable_safety_pragmas(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    try:
        assert database.engine.sync_engine.hide_parameters is True
        async with database.engine.connect() as connection:
            foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
            journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
            busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
    finally:
        await database.dispose()

    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert busy_timeout == 5000


async def test_sqlite_lock_retry_is_bounded(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    attempts = 0

    async def temporarily_locked() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError("SELECT 1", {}, Exception("database is locked"))
        return "completed"

    try:
        result = await database.retry_sqlite_locks(temporarily_locked)
    finally:
        await database.dispose()

    assert result == "completed"
    assert attempts == 3


async def test_sqlite_lock_retry_does_not_mask_other_database_errors(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    attempts = 0

    async def invalid_query() -> None:
        nonlocal attempts
        attempts += 1
        raise OperationalError("SELECT invalid", {}, Exception("syntax error"))

    try:
        with pytest.raises(OperationalError):
            await database.retry_sqlite_locks(invalid_query)
    finally:
        await database.dispose()

    assert attempts == 1


def test_postgres_engine_uses_bounded_runtime_pool() -> None:
    database = Database("postgresql+asyncpg://user:password@localhost/suppsystem")
    try:
        assert database.engine.sync_engine.hide_parameters is True
        assert database.engine.pool.size() == POSTGRES_POOL_SIZE
    finally:
        # Engine construction does not open a connection; avoid requiring an event loop here.
        database.engine.sync_engine.dispose(close=False)
