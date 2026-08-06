from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass
class _TicketLockEntry:
    lock: asyncio.Lock
    users: int = 0


class TicketLockPool:
    """Per-ticket locks that disappear after active and waiting users leave."""

    def __init__(self) -> None:
        self._entries: dict[int | str, _TicketLockEntry] = {}
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, ticket_key: int | str) -> AsyncIterator[None]:
        async with self._guard:
            entry = self._entries.get(ticket_key)
            if entry is None:
                entry = _TicketLockEntry(lock=asyncio.Lock())
                self._entries[ticket_key] = entry
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(ticket_key) is entry:
                    del self._entries[ticket_key]

    def __len__(self) -> int:
        return len(self._entries)
