from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from suppsystem.database import Database
from suppsystem.telegram_system_topics import (
    RATINGS_TOPIC,
    RATINGS_TOPIC_NAME,
    TelegramSystemTopicService,
)
from suppsystem.web_models import SystemSetting


class FakeLimiter:
    def __init__(self) -> None:
        self.wait_count = 0

    async def wait(self) -> None:
        self.wait_count += 1


class RecordingTopicBot:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self._next_topic_id = 900

    async def create_forum_topic(self, **kwargs: object) -> SimpleNamespace:
        self._next_topic_id += 1
        self.created.append(kwargs)
        return SimpleNamespace(message_thread_id=self._next_topic_id)


async def test_ratings_topic_is_created_once_persisted_and_recovered(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/system-topics.db")
    await database.create_schema_for_tests()
    try:
        bot = RecordingTopicBot()
        limiter = FakeLimiter()
        service = TelegramSystemTopicService(
            bot=bot,  # type: ignore[arg-type]
            database=database,
            support_group_id=-100123,
            limiter=limiter,  # type: ignore[arg-type]
        )

        first, concurrent = await asyncio.gather(
            service.ensure(RATINGS_TOPIC),
            service.ensure(RATINGS_TOPIC),
        )

        assert first == concurrent == 901
        assert bot.created == [{"chat_id": -100123, "name": RATINGS_TOPIC_NAME}]
        assert limiter.wait_count == 1

        restarted = TelegramSystemTopicService(
            bot=bot,  # type: ignore[arg-type]
            database=database,
            support_group_id=-100123,
            limiter=limiter,  # type: ignore[arg-type]
        )
        assert await restarted.ensure(RATINGS_TOPIC) == 901
        assert len(bot.created) == 1

        assert await restarted.recover(RATINGS_TOPIC, 901) == 902
        assert len(bot.created) == 2

        async with database.session() as session:
            setting = await session.get(SystemSetting, "telegram_topic:-100123:ratings")
        assert setting is not None
        assert setting.value == "902"
    finally:
        await database.dispose()


async def test_unknown_system_topic_is_rejected(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/unknown-system-topic.db")
    await database.create_schema_for_tests()
    try:
        service = TelegramSystemTopicService(
            bot=RecordingTopicBot(),  # type: ignore[arg-type]
            database=database,
            support_group_id=-100123,
            limiter=FakeLimiter(),  # type: ignore[arg-type]
        )

        with pytest.raises(ValueError, match="unsupported system topic"):
            await service.ensure("unknown")
    finally:
        await database.dispose()
