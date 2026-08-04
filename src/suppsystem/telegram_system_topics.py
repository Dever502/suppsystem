from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from suppsystem.database import Database, retry_sqlite_locks
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.web_models import SystemSetting

logger = logging.getLogger(__name__)

RATINGS_TOPIC = "ratings"
RATINGS_TOPIC_NAME = "⭐ Оценки"
QUICK_REPLIES_TOPIC = "quick_replies"
QUICK_REPLIES_TOPIC_NAME = "📚 Готовые ответы"


class TelegramSystemTopicService:
    """Lazily provisions durable, non-ticket Forum topics."""

    def __init__(
        self,
        *,
        bot: Bot,
        database: Database,
        support_group_id: int,
        limiter: TelegramRateLimiter,
    ) -> None:
        self.bot = bot
        self.database = database
        self.support_group_id = support_group_id
        self.limiter = limiter
        self._lock = asyncio.Lock()

    def _setting_key(self, topic_kind: str) -> str:
        return f"telegram_topic:{self.support_group_id}:{topic_kind}"

    @staticmethod
    def _topic_name(topic_kind: str) -> str:
        topic_names = {
            RATINGS_TOPIC: RATINGS_TOPIC_NAME,
            QUICK_REPLIES_TOPIC: QUICK_REPLIES_TOPIC_NAME,
        }
        try:
            return topic_names[topic_kind]
        except KeyError:
            raise ValueError(f"unsupported system topic: {topic_kind}") from None

    async def _stored_topic_id(self, topic_kind: str) -> int | None:
        async with self.database.session() as session:
            setting = await session.get(SystemSetting, self._setting_key(topic_kind))
        if setting is None:
            return None
        try:
            topic_id = int(setting.value)
        except ValueError:
            logger.error(
                "Ignored malformed system topic state",
                extra={
                    "event": "system_topic_state_invalid",
                    "system_topic": topic_kind,
                    "support_group_id": self.support_group_id,
                },
            )
            return None
        return topic_id if topic_id > 0 else None

    @retry_sqlite_locks
    async def _save_topic_id(self, topic_kind: str, topic_id: int) -> None:
        async with self.database.session() as session:
            key = self._setting_key(topic_kind)
            setting = await session.get(SystemSetting, key)
            if setting is None:
                session.add(SystemSetting(key=key, value=str(topic_id)))
            else:
                setting.value = str(topic_id)
            await session.commit()

    async def _create_topic(self, topic_kind: str) -> int:
        await self.limiter.wait()
        topic = await self.bot.create_forum_topic(
            chat_id=self.support_group_id,
            name=self._topic_name(topic_kind),
        )
        topic_id = int(topic.message_thread_id)
        await self._save_topic_id(topic_kind, topic_id)
        logger.info(
            "Created Telegram system topic",
            extra={
                "event": "system_topic_created",
                "system_topic": topic_kind,
                "support_group_id": self.support_group_id,
                "topic_id": topic_id,
            },
        )
        return topic_id

    async def ensure(self, topic_kind: str) -> int:
        self._topic_name(topic_kind)
        async with self._lock:
            stored_topic_id = await self._stored_topic_id(topic_kind)
            if stored_topic_id is not None:
                return stored_topic_id
            return await self._create_topic(topic_kind)

    async def recover(self, topic_kind: str, missing_topic_id: int) -> int:
        self._topic_name(topic_kind)
        async with self._lock:
            stored_topic_id = await self._stored_topic_id(topic_kind)
            if stored_topic_id is not None and stored_topic_id != missing_topic_id:
                return stored_topic_id
            replacement_topic_id = await self._create_topic(topic_kind)
            logger.warning(
                "Recreated missing Telegram system topic",
                extra={
                    "event": "system_topic_recreated",
                    "system_topic": topic_kind,
                    "support_group_id": self.support_group_id,
                    "old_topic_id": missing_topic_id,
                    "new_topic_id": replacement_topic_id,
                },
            )
            return replacement_topic_id
