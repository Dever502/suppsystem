from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from postgres_support import disposable_postgres_database


@pytest.fixture
async def postgres_database_url() -> AsyncIterator[str]:
    async for database_url in disposable_postgres_database():
        yield database_url
