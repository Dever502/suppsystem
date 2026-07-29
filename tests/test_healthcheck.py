from __future__ import annotations

import asyncio
import os
from pathlib import Path

from supportbot.healthcheck import heartbeats_healthy
from supportbot.heartbeat import Heartbeat
from supportbot.notification_webhook import NotificationWebhookWorker
from supportbot.runtime_health import ComponentStatus, RuntimeHealth


def touch_at(path: Path, timestamp: float) -> None:
    path.touch()
    os.utime(path, (timestamp, timestamp))


def test_healthcheck_requires_webhook_heartbeat_only_when_enabled(tmp_path: Path) -> None:
    now = 1_000.0
    touch_at(tmp_path / "heartbeat", now)
    touch_at(tmp_path / "delivery-worker-heartbeat", now)

    assert heartbeats_healthy(tmp_path, notification_webhook_enabled=False, now=now) is True
    assert heartbeats_healthy(tmp_path, notification_webhook_enabled=True, now=now) is False

    touch_at(tmp_path / "notification-webhook-worker-heartbeat", now)

    assert heartbeats_healthy(tmp_path, notification_webhook_enabled=True, now=now) is True


def test_healthcheck_rejects_stale_webhook_heartbeat(tmp_path: Path) -> None:
    now = 1_000.0
    touch_at(tmp_path / "heartbeat", now)
    touch_at(tmp_path / "delivery-worker-heartbeat", now)
    touch_at(tmp_path / "notification-webhook-worker-heartbeat", now - 46)

    assert heartbeats_healthy(tmp_path, notification_webhook_enabled=True, now=now) is False


def test_notification_worker_touches_heartbeat(tmp_path: Path) -> None:
    heartbeat = tmp_path / "nested" / "notification-heartbeat"
    worker = object.__new__(NotificationWebhookWorker)
    worker.heartbeat_path = heartbeat

    worker._touch_heartbeat()

    assert heartbeat.exists()


def test_runtime_health_reports_configured_and_disabled_components() -> None:
    health = RuntimeHealth()
    health.register("database")
    health.register("panel", configured=False)

    starting = health.snapshot(now=100)
    health.ready("database")
    ready = health.snapshot(now=100)

    assert starting.ready is False
    assert starting.components == {
        "database": ComponentStatus.STARTING,
        "panel": ComponentStatus.NOT_CONFIGURED,
    }
    assert ready.ready is True
    assert ready.components["panel"] is ComponentStatus.NOT_CONFIGURED


def test_runtime_health_degrades_stale_progress() -> None:
    health = RuntimeHealth()
    health.register("delivery_worker", progress_timeout_seconds=45)
    health.progress("delivery_worker", now=100)

    fresh = health.snapshot(now=145)
    stale = health.snapshot(now=145.01)

    assert fresh.ready is True
    assert fresh.components["delivery_worker"] is ComponentStatus.READY
    assert stale.ready is False
    assert stale.components["delivery_worker"] is ComponentStatus.DEGRADED


def test_runtime_health_recovers_after_new_progress() -> None:
    health = RuntimeHealth()
    health.register("worker", progress_timeout_seconds=10)
    health.progress("worker", now=5)
    assert health.snapshot(now=20).ready is False

    health.progress("worker", now=21)

    assert health.snapshot(now=21).ready is True


def test_runtime_readiness_requires_every_required_worker() -> None:
    health = RuntimeHealth()
    for component in ("telegram_ingress", "reconciliation", "delivery_worker"):
        health.register(component, progress_timeout_seconds=45)
        health.progress(component, now=100)

    assert health.is_ready(now=145) is True

    health.progress("delivery_worker", now=146)

    assert health.is_ready(now=146) is False
    assert health.snapshot(now=146).components["telegram_ingress"] is ComponentStatus.DEGRADED


async def test_main_heartbeat_requires_runtime_progress(tmp_path: Path) -> None:
    progressing = False
    heartbeat = Heartbeat(
        tmp_path / "heartbeat",
        interval_seconds=0.01,
        progress_probe=lambda: progressing,
    )
    task = asyncio.create_task(heartbeat.run())
    await asyncio.sleep(0.02)
    assert heartbeat.path.exists() is False

    progressing = True
    await asyncio.sleep(0.02)
    heartbeat.stop()
    await task

    assert heartbeat.path.exists() is True
