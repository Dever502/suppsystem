from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.types import Update
from fastapi import FastAPI
from pydantic import SecretStr

from supportbot.api_server import ApiServer
from supportbot.config import Settings
from supportbot.runtime_health import ComponentStatus, RuntimeHealth
from supportbot.runtime_supervision import (
    shutdown_runtime,
    stop_polling_task,
    supervise_ingress,
)
from supportbot.telegram_ingress import DurableTelegramIngressMiddleware
from supportbot.telegram_lifecycle import (
    TelegramUpdateTaskRegistry,
    create_polling_task,
)


def _settings() -> Settings:
    return Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_host="127.0.0.1",
        api_port=8765,
    )


async def test_unexpected_api_exit_is_degraded_and_propagated() -> None:
    health = RuntimeHealth()
    health.register("api")
    api_server = ApiServer(FastAPI(), _settings(), health)
    api_server.server.serve = AsyncMock(return_value=None)

    assert api_server.server.config.proxy_headers is False
    assert api_server.server.config.timeout_graceful_shutdown == 20

    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        await api_server.start()

    assert health.snapshot().components["api"] is ComponentStatus.DEGRADED


async def test_api_failure_stops_polling_and_reaches_process_supervisor() -> None:
    polling_stopped = asyncio.Event()

    async def poll() -> None:
        await polling_stopped.wait()

    async def fail_api() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("api failed")

    async def stop_polling() -> None:
        polling_stopped.set()

    polling_task = asyncio.create_task(poll())
    api_task = asyncio.create_task(fail_api())

    with pytest.raises(RuntimeError, match="api failed"):
        await supervise_ingress(polling_task, api_task, stop_polling)

    assert polling_stopped.is_set()
    assert polling_task.done()


async def test_clean_unexpected_api_stop_also_terminates_polling() -> None:
    polling_stopped = asyncio.Event()

    async def poll() -> None:
        await polling_stopped.wait()

    async def stop_api() -> None:
        await asyncio.sleep(0)

    async def stop_polling() -> None:
        polling_stopped.set()

    with pytest.raises(RuntimeError, match="API server stopped"):
        await supervise_ingress(
            asyncio.create_task(poll()),
            asyncio.create_task(stop_api()),
            stop_polling,
        )

    assert polling_stopped.is_set()


async def test_early_shutdown_cancels_polling_before_sessions_can_close() -> None:
    polling_task = asyncio.create_task(asyncio.Event().wait())

    async def not_running_yet() -> None:
        raise RuntimeError("Polling is not started")

    await stop_polling_task(polling_task, not_running_yet)

    assert polling_task.cancelled()


async def test_polling_keeps_bot_session_owned_by_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher = Dispatcher()
    captured: dict[str, Any] = {}

    async def start_polling(*bots: object, **kwargs: object) -> None:
        captured["bots"] = bots
        captured.update(kwargs)

    monkeypatch.setattr(dispatcher, "start_polling", start_polling)
    bot = object()

    task = create_polling_task(
        dispatcher,
        bot,  # type: ignore[arg-type]
        allowed_updates=["message"],
    )
    await task

    assert captured["bots"] == (bot,)
    assert captured["close_bot_session"] is False
    assert captured["handle_as_tasks"] is False


def test_telegram_task_registry_fails_fast_on_incompatible_aiogram() -> None:
    with pytest.raises(RuntimeError, match="incompatible with safe Telegram update shutdown"):
        TelegramUpdateTaskRegistry(object())  # type: ignore[arg-type]


async def test_registry_drains_task_created_before_middleware_entry() -> None:
    dispatcher = Dispatcher()
    registry = TelegramUpdateTaskRegistry(dispatcher)
    release = asyncio.Event()

    async def not_entered_middleware_yet() -> None:
        await release.wait()

    handler_task = asyncio.create_task(not_entered_middleware_yet())
    dispatcher_tasks = dispatcher._handle_update_tasks
    dispatcher_tasks.add(handler_task)
    handler_task.add_done_callback(dispatcher_tasks.discard)

    drain_task = asyncio.create_task(registry.drain())
    await asyncio.sleep(0)

    assert not drain_task.done()
    release.set()
    await drain_task
    assert handler_task.done()


async def test_shutdown_waits_for_handlers_api_and_workers_before_resources(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = Dispatcher()
    registry = TelegramUpdateTaskRegistry(dispatcher)
    handler_release = asyncio.Event()
    api_release = asyncio.Event()
    worker_stopped = asyncio.Event()
    events: list[str] = []

    async def slow_handler(event: Update, data: dict[str, Any]) -> None:
        del event, data
        try:
            await handler_release.wait()
        except asyncio.CancelledError:  # pragma: no cover - regression sentinel
            events.append("handler_cancelled")
            raise
        events.append("handler_done")

    handler_task = asyncio.create_task(registry(slow_handler, Update(update_id=1), {}))
    await asyncio.sleep(0)

    async def api_request() -> None:
        await api_release.wait()
        events.append("api_done")

    async def worker() -> None:
        await worker_stopped.wait()
        events.append("worker_done")

    async def close(name: str) -> None:
        events.append(name)

    def request_api_stop() -> None:
        events.append("api_stop_requested")

    def stop_worker() -> None:
        events.append("worker_stop")
        worker_stopped.set()

    polling_task = asyncio.create_task(asyncio.sleep(0))
    await polling_task
    api_task = asyncio.create_task(api_request(), name="api-server")
    worker_task = asyncio.create_task(worker(), name="delivery-worker")

    caplog.set_level(logging.WARNING)
    shutdown_task = asyncio.create_task(
        shutdown_runtime(
            polling_task=polling_task,
            stop_polling=AsyncMock(),
            drain_telegram_handlers=registry.drain,
            api_task=api_task,
            request_api_stop=request_api_stop,
            worker_tasks=(worker_task,),
            stop_workers=(stop_worker,),
            close_resources=(
                lambda: close("bot_close"),
                lambda: close("http_close"),
                lambda: close("database_dispose"),
            ),
            soft_timeout_seconds=0.01,
        )
    )
    await asyncio.sleep(0.03)

    assert not handler_task.cancelled()
    assert any(
        getattr(record, "event", None) == "shutdown_soft_deadline_exceeded"
        for record in caplog.records
    )
    assert "worker_stop" not in events
    assert "bot_close" not in events

    handler_release.set()
    await handler_task
    await asyncio.sleep(0)
    assert "worker_stop" not in events

    api_release.set()
    await shutdown_task

    assert "handler_cancelled" not in events
    assert events.index("api_done") < events.index("worker_stop")
    assert events.index("worker_done") < events.index("bot_close")
    assert events[-3:] == ["bot_close", "http_close", "database_dispose"]


@pytest.mark.parametrize(
    "compose_file", ("compose.production.sqlite.yaml", "compose.production.postgres.yaml")
)
def test_compose_allows_shutdown_to_outlive_soft_deadline(compose_file: str) -> None:
    content = Path(compose_file).read_text()

    assert "stop_grace_period: 90s" in content


async def test_durable_ingress_commits_without_running_handler_then_replays() -> None:
    class Repository:
        def __init__(self) -> None:
            self.saved: list[tuple[int, dict[str, object]]] = []

        async def enqueue_inbound_update(self, update_id: int, payload: dict[str, object]) -> bool:
            self.saved.append((update_id, payload))
            return True

    repository = Repository()
    wake_count = 0

    def wake() -> None:
        nonlocal wake_count
        wake_count += 1

    middleware = DurableTelegramIngressMiddleware(
        repository,
        wake,  # type: ignore[arg-type]
    )
    update = Update.model_validate({"update_id": 501})
    handled: list[int] = []

    async def handler(event: object, data: dict[str, Any]) -> str:
        assert isinstance(event, Update)
        handled.append(event.update_id)
        return "handled"

    assert await middleware(handler, update, {}) is None  # type: ignore[arg-type]
    assert handled == []
    assert repository.saved[0][0] == 501
    assert repository.saved[0][1]["update_id"] == 501
    assert wake_count == 1

    result = await middleware(  # type: ignore[arg-type]
        handler, update, {"durable_replay": True}
    )
    assert result == "handled"
    assert handled == [501]
