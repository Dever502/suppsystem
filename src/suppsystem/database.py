from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Concatenate, Protocol

from sqlalchemy import event, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from suppsystem.models import Base


class DatabaseOwner(Protocol):
    database: Database


def retry_sqlite_locks[Owner: DatabaseOwner, **P, R](
    method: Callable[Concatenate[Owner, P], Awaitable[R]],
) -> Callable[Concatenate[Owner, P], Awaitable[R]]:
    @wraps(method)
    async def wrapper(owner: Owner, /, *args: P.args, **kwargs: P.kwargs) -> R:
        return await owner.database.retry_sqlite_locks(lambda: method(owner, *args, **kwargs))

    return wrapper


def configure_sqlite_connection(
    dbapi_connection: Any,
    connection_record: Any,  # noqa: ARG001
) -> None:
    """Apply required safety and concurrency settings to every SQLite connection."""

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class Database:
    def __init__(self, database_url: str) -> None:
        self.is_sqlite = make_url(database_url).get_backend_name() == "sqlite"
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        if self.is_sqlite:
            event.listen(self.engine.sync_engine, "connect", configure_sqlite_connection)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema_for_tests(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.sessions()

    async def retry_sqlite_locks[R](
        self,
        operation: Callable[[], Awaitable[R]],
        *,
        max_attempts: int = 3,
    ) -> R:
        for attempt in range(max_attempts):
            try:
                return await operation()
            except OperationalError as error:
                message = str(error).casefold()
                locked = "database is locked" in message or "database table is locked" in message
                if not self.is_sqlite or not locked or attempt + 1 >= max_attempts:
                    raise
                await asyncio.sleep(0.05 * (2**attempt))
        raise RuntimeError("unreachable SQLite retry state")
