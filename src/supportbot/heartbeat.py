from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path


class Heartbeat:
    def __init__(
        self,
        path: Path,
        interval_seconds: float = 15.0,
        progress_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.path = path
        self.interval_seconds = interval_seconds
        self.progress_probe = progress_probe
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while not self._stopped.is_set():
            if self.progress_probe is None or self.progress_probe():
                self.path.touch()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopped.set()
