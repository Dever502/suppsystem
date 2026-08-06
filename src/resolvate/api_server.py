from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI

from resolvate.config import Settings
from resolvate.runtime_health import RuntimeHealth


class ApiServer:
    def __init__(
        self, app: FastAPI, settings: Settings, runtime_health: RuntimeHealth | None = None
    ) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.api_host,
                port=settings.api_port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=20,
                # The application applies API_TRUSTED_PROXY_IPS itself. Letting
                # Uvicorn rewrite request.client first would bypass that policy.
                proxy_headers=False,
            )
        )
        self._task: asyncio.Task[None] | None = None
        self.runtime_health = runtime_health

    def start(self) -> asyncio.Task[None]:
        if self._task is not None:
            raise RuntimeError("API server is already started")
        self._task = asyncio.create_task(self._serve(), name="api-server")
        return self._task

    async def _serve(self) -> None:
        try:
            await self.server.serve()
            if not self.server.should_exit:
                if self.runtime_health is not None:
                    self.runtime_health.degraded("api")
                raise RuntimeError("API server stopped unexpectedly")
        except BaseException:
            if self.runtime_health is not None:
                self.runtime_health.degraded("api")
            raise

    def request_stop(self) -> None:
        self.server.should_exit = True

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def stop(self) -> None:
        self.request_stop()
        await self.wait()
