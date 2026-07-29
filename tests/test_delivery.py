from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CopyMessage
from pydantic import SecretStr

from supportbot.config import Settings
from supportbot.delivery import DeliveryWorker
from supportbot.models import Direction
from supportbot.runtime_health import ComponentStatus, RuntimeHealth
from supportbot.services import DeliveryJob


class MissingTopicBot:
    async def copy_message(self, **kwargs: object) -> None:
        raise TelegramBadRequest(
            method=CopyMessage(chat_id=1, from_chat_id=2, message_id=3),
            message="message thread not found",
        )


class SuccessfulBot:
    async def copy_message(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(message_id=456)

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(message_id=789)


class RecordingTextBot(SuccessfulBot):
    def __init__(self) -> None:
        self.send_calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        self.send_calls.append(kwargs)
        return await super().send_message(**kwargs)


class BotMustNotSend:
    async def copy_message(self, **kwargs: object) -> None:
        raise AssertionError("blocked delivery reached Telegram copy_message")

    async def send_message(self, **kwargs: object) -> None:
        raise AssertionError("blocked delivery reached Telegram send_message")


class FakeLimiter:
    async def wait(self) -> None:
        return None

    async def defer(self, seconds: float) -> None:
        return None


class FakeTicketService:
    def __init__(
        self,
        *,
        retargeted: int = 1,
        blocked_user_ids: set[int] | None = None,
        transitions_applied: bool = True,
    ) -> None:
        self.retry_calls: list[tuple[str, dict[str, Any]]] = []
        self.retarget_calls: list[dict[str, Any]] = []
        self.delivered_calls: list[tuple[str, str, int | None]] = []
        self.cancelled_calls: list[tuple[str, str, str]] = []
        self.released_claims: list[tuple[str, str]] = []
        self.retargeted = retargeted
        self.blocked_user_ids = blocked_user_ids or set()
        self.transitions_applied = transitions_applied

    async def is_blocked(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.blocked_user_ids

    async def mark_delivery_cancelled(
        self, delivery_id: str, *, claim_token: str, reason: str
    ) -> bool:
        self.cancelled_calls.append((delivery_id, claim_token, reason))
        return self.transitions_applied

    async def mark_delivery_retry(self, delivery_id: str, **kwargs: Any) -> bool:
        self.retry_calls.append((delivery_id, kwargs))
        return self.transitions_applied

    async def retarget_topic_deliveries(self, **kwargs: Any) -> int:
        self.retarget_calls.append(kwargs)
        return self.retargeted

    async def mark_delivery_delivered(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        delivered_message_id: int | None = None,
    ) -> bool:
        self.delivered_calls.append((delivery_id, claim_token, delivered_message_id))
        return self.transitions_applied

    async def release_delivery_claims(self, claims: list[tuple[str, str]]) -> int:
        self.released_claims.extend(claims)
        return len(claims)


def settings() -> Settings:
    return Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        delivery_max_attempts=8,
    )


def delivery_job() -> DeliveryJob:
    return DeliveryJob(
        id="delivery-1",
        ticket_id="ticket-1",
        payload={
            "kind": "copy",
            "target_chat_id": -100123,
            "target_thread_id": 900,
            "source_chat_id": 123,
            "source_message_id": 5,
        },
        attempt_count=1,
        claim_token="claim-1",
    )


async def recovery_must_not_run(ticket_id: str, old_topic_id: int) -> int | None:
    raise AssertionError("recovery should not run")


def delivery_worker(
    tmp_path: Path,
    *,
    service: Any,
    bot: Any,
    recovery: Any = recovery_must_not_run,
    worker_type: type[DeliveryWorker] = DeliveryWorker,
    **kwargs: Any,
) -> DeliveryWorker:
    return worker_type(
        bot=bot,
        ticket_service=service,
        outbox=service,
        settings=settings(),
        limiter=FakeLimiter(),
        heartbeat_path=tmp_path / "delivery.heartbeat",
        recover_missing_topic=recovery,
        **kwargs,
    )


async def test_stopping_worker_releases_only_unstarted_claims(tmp_path: Path) -> None:
    service = FakeTicketService()
    worker = delivery_worker(tmp_path, service=service, bot=BotMustNotSend())
    jobs = [
        delivery_job(),
        DeliveryJob(
            id="delivery-2",
            ticket_id="ticket-2",
            payload=delivery_job().payload,
            attempt_count=1,
            claim_token="claim-2",
        ),
    ]

    worker.stop()
    await worker._process_claimed_jobs(jobs)

    assert service.released_claims == [
        ("delivery-1", "claim-1"),
        ("delivery-2", "claim-2"),
    ]
    assert service.delivered_calls == []


async def test_worker_finishes_current_claim_then_releases_remaining_claims(
    tmp_path: Path,
) -> None:
    service = FakeTicketService()

    class StopAfterCurrentCopy(SuccessfulBot):
        worker: DeliveryWorker

        async def copy_message(self, **kwargs: object) -> SimpleNamespace:
            self.worker.stop()
            return await super().copy_message(**kwargs)

    bot = StopAfterCurrentCopy()
    worker = delivery_worker(tmp_path, service=service, bot=bot)
    bot.worker = worker
    second = DeliveryJob(
        id="delivery-2",
        ticket_id="ticket-2",
        payload=delivery_job().payload,
        attempt_count=1,
        claim_token="claim-2",
    )

    await worker._process_claimed_jobs([delivery_job(), second])

    assert service.delivered_calls == [("delivery-1", "claim-1", 456)]
    assert service.released_claims == [("delivery-2", "claim-2")]


async def test_blocked_recipient_cancels_outgoing_delivery(tmp_path: Path) -> None:
    service = FakeTicketService(blocked_user_ids={123})
    worker = delivery_worker(tmp_path, service=service, bot=BotMustNotSend())

    await worker._deliver(
        DeliveryJob(
            id="delivery-1",
            ticket_id="ticket-1",
            direction=Direction.OPERATOR_TO_USER,
            payload={"kind": "send_text", "target_chat_id": 123, "text": "blocked"},
            attempt_count=1,
            claim_token="claim-1",
        )
    )

    assert service.cancelled_calls == [("delivery-1", "claim-1", "recipient is blocked")]
    assert service.delivered_calls == []
    assert service.retry_calls == []


async def test_successful_delivery_persists_telegram_message_id(tmp_path: Path) -> None:
    service = FakeTicketService()
    worker = delivery_worker(tmp_path, service=service, bot=SuccessfulBot())

    await worker._deliver(delivery_job())

    assert service.delivered_calls == [("delivery-1", "claim-1", 456)]


async def test_text_delivery_uses_only_explicit_html_parse_mode(tmp_path: Path) -> None:
    service = FakeTicketService()
    bot = RecordingTextBot()
    worker = delivery_worker(tmp_path, service=service, bot=bot)

    for delivery_id, parse_mode in (("plain", None), ("html", "HTML")):
        payload: dict[str, object] = {
            "kind": "send_text",
            "target_chat_id": 123,
            "text": "<b>Message</b>",
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        await worker._deliver(
            DeliveryJob(
                id=delivery_id,
                ticket_id="ticket-1",
                payload=payload,
                attempt_count=1,
                claim_token=f"claim-{delivery_id}",
            )
        )

    assert [call["parse_mode"] for call in bot.send_calls] == [None, "HTML"]


async def test_stale_success_is_not_reported_as_delivered(tmp_path: Path, caplog: Any) -> None:
    service = FakeTicketService(transitions_applied=False)
    worker = delivery_worker(tmp_path, service=service, bot=SuccessfulBot())

    with caplog.at_level(logging.INFO, logger="supportbot.delivery"):
        await worker._deliver(delivery_job())

    events = [getattr(record, "event", None) for record in caplog.records]
    assert "stale_delivery_claim_transition_ignored" in events
    assert "delivery_delivered" not in events


def test_delivery_worker_records_runtime_progress(tmp_path: Path) -> None:
    health = RuntimeHealth()
    health.register("delivery_worker", progress_timeout_seconds=45)
    worker = object.__new__(DeliveryWorker)
    worker.heartbeat_path = tmp_path / "delivery.heartbeat"
    worker.runtime_health = health

    worker._record_progress()

    snapshot = health.snapshot()
    assert snapshot.ready is True
    assert snapshot.components["delivery_worker"] is ComponentStatus.READY


async def test_delivery_worker_recovers_stale_claims_periodically(tmp_path: Path) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    class PollingService:
        def __init__(self, clock: Clock) -> None:
            self.clock = clock
            self.worker: DeliveryWorker | None = None
            self.claim_calls = 0
            self.release_calls: list[int] = []

        async def release_stale_deliveries(self, stale_after_seconds: int = 300) -> int:
            self.release_calls.append(stale_after_seconds)
            return 0

        async def claim_due_deliveries(self) -> list[DeliveryJob]:
            self.claim_calls += 1
            self.clock.advance(5.0)
            if self.claim_calls == 3:
                assert self.worker is not None
                self.worker.stop()
            return []

    class NoWaitDeliveryWorker(DeliveryWorker):
        async def _wait_for_poll_interval(self, *, delay_seconds: float | None = None) -> None:
            del delay_seconds

    clock = Clock()
    service = PollingService(clock)
    worker = delivery_worker(
        tmp_path,
        service=service,
        bot=BotMustNotSend(),
        worker_type=NoWaitDeliveryWorker,
        stale_recovery_interval_seconds=10.0,
        stale_delivery_after_seconds=77,
        monotonic=clock,
    )
    service.worker = worker

    await worker.run()

    assert service.claim_calls == 3
    assert service.release_calls == [77, 77]


async def test_missing_topic_recovery_failure_requeues_delivery_immediately(
    tmp_path: Path,
) -> None:
    service = FakeTicketService()

    async def fail_recovery(ticket_id: str, old_topic_id: int) -> int | None:
        raise RuntimeError("topic creation failed")

    worker = delivery_worker(
        tmp_path, service=service, bot=MissingTopicBot(), recovery=fail_recovery
    )

    await worker._deliver(delivery_job())

    assert service.retarget_calls == []
    assert service.retry_calls == [
        (
            "delivery-1",
            {
                "error": "topic recovery failed: topic creation failed",
                "retry_after_seconds": 5,
                "max_attempts": 8,
                "claim_token": "claim-1",
            },
        )
    ]


async def test_missing_topic_recovery_retargets_unfinished_deliveries(tmp_path: Path) -> None:
    service = FakeTicketService()

    async def recover(ticket_id: str, old_topic_id: int) -> int | None:
        assert (ticket_id, old_topic_id) == ("ticket-1", 900)
        return 901

    worker = delivery_worker(tmp_path, service=service, bot=MissingTopicBot(), recovery=recover)

    await worker._deliver(delivery_job())

    assert service.retry_calls == []
    assert service.retarget_calls == [
        {
            "ticket_id": "ticket-1",
            "old_topic_id": 900,
            "new_topic_id": 901,
        }
    ]


async def test_missing_topic_without_replacement_requeues_delivery(tmp_path: Path) -> None:
    service = FakeTicketService()

    async def no_replacement(ticket_id: str, old_topic_id: int) -> int | None:
        return None

    worker = delivery_worker(
        tmp_path, service=service, bot=MissingTopicBot(), recovery=no_replacement
    )

    await worker._deliver(delivery_job())

    assert service.retarget_calls == []
    assert service.retry_calls[0][0] == "delivery-1"
    assert service.retry_calls[0][1]["retry_after_seconds"] == 5


async def test_empty_retarget_result_requeues_current_delivery(tmp_path: Path) -> None:
    service = FakeTicketService(retargeted=0)

    async def recover(ticket_id: str, old_topic_id: int) -> int | None:
        return 901

    worker = delivery_worker(tmp_path, service=service, bot=MissingTopicBot(), recovery=recover)

    await worker._deliver(delivery_job())

    assert service.retry_calls == [
        (
            "delivery-1",
            {
                "error": "topic recovered but delivery was not retargeted",
                "retry_after_seconds": 5,
                "max_attempts": 8,
                "claim_token": "claim-1",
            },
        )
    ]
