from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

POSTGRES_TEST_DATABASE_ENV = "TEST_POSTGRES_DATABASE_URL"
POSTGRES_TEST_RESET_GUARD_ENV = "ALLOW_POSTGRES_TEST_DATABASE_CREATION"
POSTGRES_TEST_DATABASE_NAME = "resolvate_test"
POSTGRES_TEST_USERNAME = "resolvate_test"
_SAFE_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def configured_postgres_admin_url() -> str:
    database_url = os.getenv(POSTGRES_TEST_DATABASE_ENV)
    if not database_url:
        pytest.skip(f"{POSTGRES_TEST_DATABASE_ENV} is not configured; run `make test-postgres`")
    if os.getenv(POSTGRES_TEST_RESET_GUARD_ENV) != "yes":
        raise pytest.UsageError(
            f"{POSTGRES_TEST_RESET_GUARD_ENV}=yes is required for PostgreSQL tests"
        )

    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg":
        raise pytest.UsageError(
            f"{POSTGRES_TEST_DATABASE_ENV} must use the postgresql+asyncpg driver"
        )
    if parsed.database != POSTGRES_TEST_DATABASE_NAME:
        raise pytest.UsageError(
            f"{POSTGRES_TEST_DATABASE_ENV} must target {POSTGRES_TEST_DATABASE_NAME!r}"
        )
    if parsed.username != POSTGRES_TEST_USERNAME:
        raise pytest.UsageError(f"{POSTGRES_TEST_DATABASE_ENV} must use {POSTGRES_TEST_USERNAME!r}")
    if not parsed.host:
        raise pytest.UsageError(f"{POSTGRES_TEST_DATABASE_ENV} must include a host")
    return database_url


async def _connect_with_retry(database_url: str, attempts: int = 30) -> None:
    engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        for attempt in range(attempts):
            try:
                async with engine.connect() as connection:
                    database_name, username = (
                        await connection.execute(text("SELECT current_database(), current_user"))
                    ).one()
                if database_name != POSTGRES_TEST_DATABASE_NAME:
                    raise pytest.UsageError(
                        f"PostgreSQL reported unexpected database {database_name!r}"
                    )
                if username != POSTGRES_TEST_USERNAME:
                    raise pytest.UsageError(f"PostgreSQL reported unexpected user {username!r}")
                return
            except (OSError, SQLAlchemyError):
                if attempt + 1 == attempts:
                    raise
                await asyncio.sleep(1)
    finally:
        await engine.dispose()


def _quoted_test_database(database_name: str) -> str:
    if not _SAFE_DATABASE_NAME.fullmatch(database_name):
        raise ValueError("Unsafe generated PostgreSQL test database name")
    return f'"{database_name}"'


async def disposable_postgres_database() -> AsyncIterator[str]:
    admin_url = configured_postgres_admin_url()
    await _connect_with_retry(admin_url)
    child_name = f"sbtest_{uuid.uuid4().hex}"
    quoted_child_name = _quoted_test_database(child_name)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    created = False
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE {quoted_child_name}"))
        created = True
        child_url = make_url(admin_url).set(database=child_name)
        yield child_url.render_as_string(hide_password=False)
    finally:
        try:
            if created:
                async with admin_engine.connect() as connection:
                    await connection.execute(
                        text(f"DROP DATABASE {quoted_child_name} WITH (FORCE)")
                    )
        finally:
            await admin_engine.dispose()
