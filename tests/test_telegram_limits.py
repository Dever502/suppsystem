from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

from aiogram.types import Update

from suppsystem.telegram_ingress import DurableTelegramIngressMiddleware, update_ordering_key
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.user_message_limits import UserMessageRateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_update_ordering_key_scopes_forum_topics_independently() -> None:
    def group_message(update_id: int, thread_id: int) -> Update:
        return Update.model_validate(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "message_thread_id": thread_id,
                    "date": 0,
                    "chat": {"id": -100123, "type": "supergroup", "title": "Support"},
                    "from": {"id": 7, "is_bot": False, "first_name": "Operator"},
                    "text": "reply",
                },
            }
        )

    assert update_ordering_key(group_message(1, 777)) == "chat:-100123:thread:777"
    assert update_ordering_key(group_message(2, 778)) == "chat:-100123:thread:778"


async def test_inbound_limiter_allows_burst_then_throttles_without_extending_block() -> None:
    clock = Clock()
    limiter = UserMessageRateLimiter(
        per_minute=3,
        per_hour=5,
        monotonic=clock.monotonic,
    )

    for _ in range(3):
        assert (await limiter.consume("telegram:42")).allowed is True

    first_rejection = await limiter.consume("telegram:42")
    repeated_rejection = await limiter.consume("telegram:42")

    assert first_rejection.allowed is False
    assert first_rejection.retry_after_seconds == 60
    assert first_rejection.notify_client is True
    assert repeated_rejection.allowed is False
    assert repeated_rejection.retry_after_seconds == 60
    assert repeated_rejection.notify_client is False

    clock.advance(61)
    assert (await limiter.consume("telegram:42")).allowed is True
    assert (await limiter.consume("telegram:42")).allowed is True

    hourly_rejection = await limiter.consume("telegram:42")
    assert hourly_rejection.allowed is False
    assert hourly_rejection.retry_after_seconds == 3539
    assert hourly_rejection.notify_client is True

    assert (await limiter.consume("telegram:99")).allowed is True

    clock.advance(3540)
    assert (await limiter.consume("telegram:42")).allowed is True


async def test_rejected_attempts_do_not_consume_more_capacity() -> None:
    clock = Clock()
    limiter = UserMessageRateLimiter(
        per_minute=1,
        per_hour=2,
        monotonic=clock.monotonic,
    )

    assert (await limiter.consume("telegram:42")).allowed is True
    for _ in range(100):
        assert (await limiter.consume("telegram:42")).allowed is False

    clock.advance(61)
    assert (await limiter.consume("telegram:42")).allowed is True


async def test_middleware_drops_excess_private_messages_before_persistence() -> None:
    class Repository:
        def __init__(self) -> None:
            self.saved: list[tuple[int, str]] = []

        async def enqueue_inbound_update(
            self,
            update_id: int,
            payload: dict[str, object],
            *,
            ordering_key: str,
        ) -> bool:
            self.saved.append((update_id, ordering_key))
            return True

    repository = Repository()
    wake = Mock()
    bot = AsyncMock()
    middleware = DurableTelegramIngressMiddleware(
        repository,  # type: ignore[arg-type]
        wake,
        bot=bot,
        inbound_limiter=UserMessageRateLimiter(per_minute=1, per_hour=2),
        outbound_limiter=TelegramRateLimiter(0.001),
    )

    def private_message(update_id: int) -> Update:
        return Update.model_validate(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": 0,
                    "chat": {"id": 42, "type": "private"},
                    "from": {
                        "id": 42,
                        "is_bot": False,
                        "first_name": "User",
                    },
                    "text": f"message {update_id}",
                },
            }
        )

    async def handler(event: object, data: dict[str, Any]) -> None:
        raise AssertionError("raw polling update must not run handlers directly")

    await middleware(handler, private_message(1), {})
    await middleware(handler, private_message(2), {})
    await middleware(handler, private_message(3), {})

    assert repository.saved == [(1, "chat:42:thread:0")]
    assert wake.call_count == 1
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs["text"] == (
        "Вы отправили слишком много сообщений. Следующее можно отправить через 1 мин."
    )
