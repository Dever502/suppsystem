from __future__ import annotations

import time
from pathlib import Path

from suppsystem.config import get_settings


def heartbeats_healthy(
    data_dir: Path,
    *,
    notification_webhook_enabled: bool,
    max_age_seconds: float = 45,
    now: float | None = None,
) -> bool:
    names = ["heartbeat", "delivery-worker-heartbeat"]
    if notification_webhook_enabled:
        names.append("notification-webhook-worker-heartbeat")
    current_time = time.time() if now is None else now
    return all(
        (heartbeat := data_dir / name).exists()
        and current_time - heartbeat.stat().st_mtime <= max_age_seconds
        for name in names
    )


def main() -> None:
    settings = get_settings()
    healthy = heartbeats_healthy(
        settings.data_dir,
        notification_webhook_enabled=settings.notification_webhook_enabled,
    )
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
