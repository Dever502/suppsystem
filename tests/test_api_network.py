from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
from pydantic import SecretStr

from resolvate.api import create_app
from resolvate.api_server import ApiServer
from resolvate.config import Settings
from resolvate.database import Database
from resolvate.runtime_health import RuntimeHealth
from resolvate.services import TicketService
from resolvate.version import PROJECT_VERSION

API_TOKEN = "abcdef0123456789abcdef0123456789"


def _unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ReverseProxy:
    """Small HTTP/1.1 proxy used to exercise the real network trust boundary."""

    def __init__(self, *, upstream_port: int, client_ip: str) -> None:
        self.upstream_port = upstream_port
        self.client_ip = client_ip
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    @property
    def port(self) -> int:
        assert self.server is not None
        sockets = self.server.sockets
        assert sockets
        return int(sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(
        self, downstream_reader: asyncio.StreamReader, downstream_writer: asyncio.StreamWriter
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            request_head = await downstream_reader.readuntil(b"\r\n\r\n")
            lines = request_head[:-4].split(b"\r\n")
            content_length = 0
            forwarded_headers: list[bytes] = []
            for line in lines[1:]:
                name, _, raw_value = line.partition(b":")
                normalized_name = name.strip().lower()
                if normalized_name == b"content-length":
                    content_length = int(raw_value.strip())
                if normalized_name in {
                    b"connection",
                    b"x-forwarded-for",
                    b"x-real-ip",
                }:
                    continue
                forwarded_headers.append(line)
            body = await downstream_reader.readexactly(content_length) if content_length else b""
            rewritten_request = b"\r\n".join(
                [
                    lines[0],
                    *forwarded_headers,
                    f"X-Forwarded-For: {self.client_ip}".encode(),
                    f"X-Real-IP: {self.client_ip}".encode(),
                    b"Connection: close",
                    b"",
                    b"",
                ]
            )

            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.upstream_port
            )
            upstream_writer.write(rewritten_request + body)
            await upstream_writer.drain()
            while response_chunk := await upstream_reader.read(64 * 1024):
                downstream_writer.write(response_chunk)
                await downstream_writer.drain()
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            downstream_writer.close()
            await downstream_writer.wait_closed()


async def _wait_until_started(api_server: ApiServer, task: asyncio.Task[None]) -> None:
    for _ in range(500):
        if api_server.server.started:
            return
        if task.done():
            await task
        await asyncio.sleep(0.01)
    raise TimeoutError("Uvicorn did not start")


async def test_real_uvicorn_reverse_proxy_security_and_graceful_shutdown(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/network.db")
    await database.create_schema_for_tests()
    service = TicketService(database)
    ticket = await service.open_or_reopen(
        telegram_user_id=71_001,
        display_name="Network API test",
        username=None,
    )
    token = await service.claim_topic_provisioning(ticket.id)
    assert token is not None
    ticket = await service.attach_topic(ticket.id, 81_001, token=token)

    api_port = _unused_loopback_port()
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
        api_host="127.0.0.1",
        api_port=api_port,
        api_requests_per_minute=9,
        api_trusted_proxy_ips=frozenset({"127.0.0.1"}),
    )
    health = RuntimeHealth()
    health.register("api")
    app = create_app(
        database=database,
        ticket_service=service,
        settings=settings,
        runtime_health=health,
    )
    api_server = ApiServer(app, settings, health)
    api_task = api_server.start()
    primary_proxy = _ReverseProxy(upstream_port=api_port, client_ip="198.51.100.10")
    rate_proxy = _ReverseProxy(upstream_port=api_port, client_ip="198.51.100.11")

    try:
        await _wait_until_started(api_server, api_task)
        await primary_proxy.start()
        await rate_proxy.start()
        auth_headers = {"X-API-Token": API_TOKEN}

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{primary_proxy.port}", timeout=5
        ) as client:
            unauthorized = await client.get("/api/v1/tickets")
            schema = await client.get("/openapi.json", headers=auth_headers)
            mutation_headers = {
                **auth_headers,
                "X-Idempotency-Key": "network-message-command",
            }
            first = await client.post(
                f"/api/v1/tickets/{ticket.id}/messages",
                json={"text": "one network effect"},
                headers=mutation_headers,
            )
            replay = await client.post(
                f"/api/v1/tickets/{ticket.id}/messages",
                json={"text": "one network effect"},
                headers=mutation_headers,
            )
            conflict = await client.post(
                f"/api/v1/tickets/{ticket.id}/messages",
                json={"text": "different network effect"},
                headers=mutation_headers,
            )

        assert unauthorized.status_code == 401
        assert schema.status_code == 200
        assert schema.json()["info"]["version"] == PROJECT_VERSION
        assert first.status_code == 200 and first.json() == {"changed": True}
        assert replay.status_code == 200 and replay.json() == {"changed": True}
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{rate_proxy.port}", timeout=5
        ) as client:
            responses = [
                await client.get(
                    "/api/v1/tickets",
                    headers={
                        **auth_headers,
                        "X-Forwarded-For": f"203.0.113.{index}",
                    },
                )
                for index in range(1, 7)
            ]

        assert [response.status_code for response in responses] == [200] * 5 + [429]
    finally:
        await primary_proxy.close()
        await rate_proxy.close()
        await api_server.stop()
        await database.dispose()

    assert api_task.done()
    assert api_task.exception() is None
