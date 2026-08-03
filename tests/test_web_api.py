from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from suppsystem import web_api_routes
from suppsystem.api import create_app
from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.migrations import upgrade_database
from suppsystem.models import (
    DeliveryOutbox,
    Direction,
    SupportBlock,
    TicketChannel,
    TicketMessage,
    TicketStatus,
)
from suppsystem.services import TicketService
from suppsystem.statistics import StatisticsService

WEB_TOKEN = "web-token-with-at-least-thirty-two-characters"
OPERATOR_TOKEN = "operator-token-with-at-least-thirty-two-chars"


async def _context(
    tmp_path: Path,
    *,
    user_messages_per_minute: int = 30,
    user_messages_per_hour: int = 200,
    web_identity_mode: Literal["external_id", "email"] = "external_id",
) -> tuple[httpx.AsyncClient, Database, TicketService]:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/web-api.db"
    await upgrade_database(database_url)
    database = Database(database_url)
    service = TicketService(database)
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(OPERATOR_TOKEN),
        web_api_enabled=True,
        web_api_token=SecretStr(WEB_TOKEN),
        data_dir=tmp_path,
        user_messages_per_minute=user_messages_per_minute,
        user_messages_per_hour=user_messages_per_hour,
        web_identity_mode=web_identity_mode,
    )
    app = create_app(database=database, ticket_service=service, settings=settings)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, database, service


async def test_web_message_flow_is_channel_aware_and_idempotent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, database, service = await _context(tmp_path)
    try:
        caplog.set_level(logging.INFO, logger="suppsystem.web_support_service")
        headers = {"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-message-0001"}
        payload = {
            "external_user_id": "customer-42",
            "email": "Person@Example.com",
            "display_name": "Web Person",
            "text": "Подробное описание проблемы",
        }
        first = await client.post("/api/v1/web/messages", headers=headers, json=payload)
        replay = await client.post("/api/v1/web/messages", headers=headers, json=payload)
        conflict = await client.post(
            "/api/v1/web/messages",
            headers=headers,
            json={**payload, "text": "Другая проблема"},
        )

        assert first.status_code == 200
        accepted_record = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "web_message_accepted"
        )
        assert accepted_record.conversation_created is True
        assert not hasattr(accepted_record, "email")
        assert not hasattr(accepted_record, "message_content")
        assert replay.json() == first.json()
        assert conflict.status_code == 409
        body = first.json()
        ticket_id = body["conversation"]["id"]
        ticket = await service.get_web_ticket(ticket_id)
        assert ticket.channel is TicketChannel.WEB
        assert ticket.telegram_user_id is None
        assert ticket.email == "person@example.com"

        page = await client.get(
            f"/api/v1/web/conversations/{ticket_id}/messages",
            headers={"X-API-Token": WEB_TOKEN},
        )
        assert page.status_code == 200
        assert [item["text"] for item in page.json()["items"]] == [payload["text"]]

        operator_result = await service.accept_operator_reply(
            ticket_id=ticket_id,
            operator_telegram_id=77,
            source_chat_id=-100123,
            source_message_id=9001,
            content="Ответ оператора",
            media=None,
        )
        assert operator_result.changed is True
        page = await client.get(
            f"/api/v1/web/conversations/{ticket_id}/messages",
            headers={"X-API-Token": WEB_TOKEN},
        )
        assert [item["direction"] for item in page.json()["items"]] == [
            Direction.USER_TO_OPERATOR,
            Direction.OPERATOR_TO_USER,
        ]
        async with database.session() as session:
            delivery_count = await session.scalar(
                select(func.count())
                .select_from(DeliveryOutbox)
                .where(DeliveryOutbox.ticket_id == ticket_id)
            )
            message_count = await session.scalar(
                select(func.count())
                .select_from(TicketMessage)
                .where(TicketMessage.ticket_id == ticket_id)
            )
        assert delivery_count == 1
        assert message_count == 2
    finally:
        await client.aclose()
        await database.dispose()


@pytest.mark.parametrize(
    ("identity_mode", "first_identity", "same_identity", "other_identity"),
    (
        (
            "external_id",
            {"external_user_id": "limited-user", "email": "one@example.com"},
            {"external_user_id": "limited-user", "email": "changed@example.com"},
            {"external_user_id": "other-user", "email": "other@example.com"},
        ),
        (
            "email",
            {"email": "Limited@Example.com"},
            {"email": "limited@example.com"},
            {"email": "other@example.com"},
        ),
    ),
)
async def test_web_message_rate_limit_is_per_canonical_identity(
    tmp_path: Path,
    identity_mode: Literal["external_id", "email"],
    first_identity: dict[str, str],
    same_identity: dict[str, str],
    other_identity: dict[str, str],
) -> None:
    client, database, _service = await _context(
        tmp_path,
        user_messages_per_minute=1,
        user_messages_per_hour=2,
        web_identity_mode=identity_mode,
    )
    try:
        first = await client.post(
            "/api/v1/web/messages",
            headers={
                "X-API-Token": WEB_TOKEN,
                "X-Idempotency-Key": "rate-limit-first",
            },
            json={**first_identity, "text": "Первое сообщение"},
        )
        limited = await client.post(
            "/api/v1/web/messages",
            headers={
                "X-API-Token": WEB_TOKEN,
                "X-Idempotency-Key": "rate-limit-second",
            },
            json={**same_identity, "text": "Лишнее сообщение"},
        )
        independent = await client.post(
            "/api/v1/web/messages",
            headers={
                "X-API-Token": WEB_TOKEN,
                "X-Idempotency-Key": "rate-limit-other",
            },
            json={**other_identity, "text": "Сообщение другого пользователя"},
        )

        assert first.status_code == 200
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        assert limited.json()["error"]["code"] == "rate_limited"
        assert independent.status_code == 200
    finally:
        await client.aclose()
        await database.dispose()


async def test_web_block_is_silent_idempotent_and_suppresses_delivery(
    tmp_path: Path,
) -> None:
    client, database, service = await _context(tmp_path)
    try:
        payload = {
            "external_user_id": "blocked-web-user",
            "email": "blocked@example.com",
            "display_name": "Blocked Web User",
            "text": "Первое сообщение",
        }
        created = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "blocked-create"},
            json=payload,
        )
        ticket_id = created.json()["conversation_id"]
        closed = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/close",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "blocked-close"},
        )
        assert closed.json() == {"changed": True}
        before = await service.get_web_ticket(ticket_id)
        statistics = StatisticsService(database, cache_ttl_seconds=0)
        assert (await statistics.get("today")).inbound_messages == 1

        block_headers = {
            "X-API-Token": WEB_TOKEN,
            "X-Idempotency-Key": "blocked-enable",
        }
        blocked = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/block",
            headers=block_headers,
            json={"reason": "abuse"},
        )
        block_replay = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/block",
            headers=block_headers,
            json={"reason": "abuse"},
        )
        block_conflict = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/block",
            headers=block_headers,
            json={"reason": "different"},
        )
        assert blocked.json() == {"changed": True}
        assert block_replay.json() == blocked.json()
        assert block_conflict.status_code == 409
        assert await service.is_ticket_blocked(ticket_id) is True

        hidden_headers = {
            "X-API-Token": WEB_TOKEN,
            "X-Idempotency-Key": "blocked-hidden-message",
        }
        hidden_payload = {**payload, "text": "Сообщение во время блокировки"}
        hidden = await client.post(
            "/api/v1/web/messages", headers=hidden_headers, json=hidden_payload
        )
        hidden_replay = await client.post(
            "/api/v1/web/messages", headers=hidden_headers, json=hidden_payload
        )
        assert hidden.status_code == 200
        assert hidden_replay.json() == hidden.json()
        assert hidden.json()["changed"] is True
        assert hidden.json()["created"] is False
        assert hidden.json()["reopened"] is False

        after = await service.get_web_ticket(ticket_id)
        assert after.status is TicketStatus.CLOSED
        assert after.closed_at == before.closed_at
        assert after.last_activity_at == before.last_activity_at
        async with database.session() as session:
            stored = await session.get(TicketMessage, hidden.json()["message_id"])
            assert stored is not None
            assert stored.suppressed is True
            assert await session.get(SupportBlock, ticket_id) is not None
            delivery_count = await session.scalar(
                select(func.count())
                .select_from(DeliveryOutbox)
                .where(DeliveryOutbox.ticket_id == ticket_id)
            )
        assert delivery_count == 1
        assert (await statistics.get("today", refresh=True)).inbound_messages == 1
        operator_page = await client.get(
            f"/api/v1/tickets/{ticket_id}/messages",
            headers={"X-API-Token": OPERATOR_TOKEN},
        )
        assert [item["content"] for item in operator_page.json()] == ["Первое сообщение"]
        operator_headers = {
            "X-API-Token": OPERATOR_TOKEN,
            "X-Idempotency-Key": "blocked-operator-message",
        }
        blocked_operator = await client.post(
            f"/api/v1/tickets/{ticket_id}/messages",
            headers=operator_headers,
            json={"text": "Не должно появиться после разблокировки"},
        )
        assert blocked_operator.status_code == 200
        assert blocked_operator.json() == {"changed": False}

        page = await client.get(
            f"/api/v1/web/conversations/{ticket_id}/messages",
            headers={"X-API-Token": WEB_TOKEN},
        )
        assert [item["text"] for item in page.json()["items"]] == [
            "Первое сообщение",
            "Сообщение во время блокировки",
        ]
        operator_reply = await service.accept_operator_reply(
            ticket_id=ticket_id,
            operator_telegram_id=77,
            source_chat_id=-100123,
            source_message_id=9901,
            content="Не должно уйти",
            media=None,
        )
        assert operator_reply.changed is False
        assert operator_reply.blocked is True

        unblock_headers = {
            "X-API-Token": WEB_TOKEN,
            "X-Idempotency-Key": "blocked-disable",
        }
        unblocked = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/unblock",
            headers=unblock_headers,
        )
        unblock_replay = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/unblock",
            headers=unblock_headers,
        )
        assert unblocked.json() == {"changed": True}
        assert unblock_replay.json() == unblocked.json()
        assert await service.is_ticket_blocked(ticket_id) is False
        blocked_operator_replay = await client.post(
            f"/api/v1/tickets/{ticket_id}/messages",
            headers=operator_headers,
            json={"text": "Не должно появиться после разблокировки"},
        )
        assert blocked_operator_replay.json() == {"changed": False}

        delivered = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "blocked-after"},
            json={**payload, "text": "После разблокировки"},
        )
        assert delivered.json()["reopened"] is True
        async with database.session() as session:
            delivery_count = await session.scalar(
                select(func.count())
                .select_from(DeliveryOutbox)
                .where(DeliveryOutbox.ticket_id == ticket_id)
            )
        assert delivery_count == 2
    finally:
        await client.aclose()
        await database.dispose()


async def test_block_suppresses_close_notification_and_rating(tmp_path: Path) -> None:
    client, database, service = await _context(tmp_path)
    try:
        created = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "blocked-events-create"},
            json={
                "external_user_id": "blocked-events",
                "email": "blocked-events@example.com",
                "text": "Visible",
            },
        )
        ticket_id = created.json()["conversation_id"]
        await client.post(
            f"/api/v1/web/conversations/{ticket_id}/block",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "blocked-events-enable"},
            json={"reason": "abuse"},
        )

        closed = await service.close(
            ticket_id=ticket_id,
            operator_telegram_id=77,
            idempotency_key="blocked-events-close",
            notification_text="Hidden close notification",
        )
        rated = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/rating",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "blocked-events-rating"},
            json={"score": 5},
        )

        assert closed is True
        assert rated.json() == {"changed": True}
        async with database.session() as session:
            messages = list(
                (
                    await session.scalars(
                        select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
                    )
                ).all()
            )
            deliveries = list(
                (
                    await session.scalars(
                        select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == ticket_id)
                    )
                ).all()
            )
        assert all(message.content != "Hidden close notification" for message in messages)
        rating = next(message for message in messages if message.channel == "rating")
        assert rating.suppressed is True
        assert len(deliveries) == 1
        statistics = await StatisticsService(database, cache_ttl_seconds=0).get("today")
        assert statistics.rating_count == 0
        assert statistics.average_rating is None
    finally:
        await client.aclose()
        await database.dispose()


async def test_web_close_rating_and_reopen_cycles(tmp_path: Path) -> None:
    client, database, service = await _context(tmp_path)
    try:
        created = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-cycle-create"},
            json={
                "external_user_id": "cycle-user",
                "email": "cycle@example.com",
                "text": "Первое сообщение",
            },
        )
        ticket_id = created.json()["conversation"]["id"]
        closed = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/close",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-cycle-close"},
        )
        rated = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/rating",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-cycle-rating"},
            json={"score": 5},
        )
        duplicate_rating = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/rating",
            headers={
                "X-API-Token": WEB_TOKEN,
                "X-Idempotency-Key": "web-cycle-rating-other",
            },
            json={"score": 4},
        )
        reopened = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-cycle-reopen"},
            json={
                "external_user_id": "cycle-user",
                "email": "cycle@example.com",
                "text": "Новая итерация",
            },
        )
        closed_again = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/close",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-cycle-close-2"},
        )
        rated_again = await client.post(
            f"/api/v1/web/conversations/{ticket_id}/rating",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-cycle-rating-2"},
            json={"score": 4},
        )

        assert closed.json() == {"changed": True}
        assert rated.json() == {"changed": True}
        assert duplicate_rating.json() == {"changed": False}
        assert reopened.json()["reopened"] is True
        assert closed_again.json() == {"changed": True}
        assert rated_again.json() == {"changed": True}
        ticket = await service.get_web_ticket(ticket_id)
        assert ticket.status is TicketStatus.CLOSED
        assert ticket.close_cycle == 2
        async with database.session() as session:
            deliveries = list(
                (
                    await session.scalars(
                        select(DeliveryOutbox).where(DeliveryOutbox.ticket_id == ticket_id)
                    )
                ).all()
            )
        rating_payloads = [
            delivery.payload
            for delivery in deliveries
            if delivery.payload.get("target_system_topic")
        ]
        assert [payload["target_system_topic"] for payload in rating_payloads] == [
            "ratings",
            "ratings",
        ]
    finally:
        await client.aclose()
        await database.dispose()


async def test_web_token_cannot_access_operator_api(tmp_path: Path) -> None:
    client, database, _service = await _context(tmp_path)
    try:
        operator = await client.get("/api/v1/tickets", headers={"X-API-Token": WEB_TOKEN})
        operator_health = await client.get("/health", headers={"X-API-Token": OPERATOR_TOKEN})
        web_health = await client.get("/health", headers={"X-API-Token": WEB_TOKEN})
        assert operator_health.status_code == 200
        assert web_health.status_code == 200
        web = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": OPERATOR_TOKEN, "X-Idempotency-Key": "wrong-realm"},
            json={
                "external_user_id": "wrong-token",
                "email": "wrong@example.com",
                "text": "Не должно пройти",
            },
        )
        assert operator.status_code == 401
        assert web.status_code == 401
        schema_response = await client.get("/openapi.json", headers={"X-API-Token": OPERATOR_TOKEN})
        schema = schema_response.json()
        schemes = schema["components"]["securitySchemes"]
        assert set(schemes) == {"OperatorApiToken", "WebApiToken"}
        assert schema["paths"]["/api/v1/web/messages"]["post"]["security"] == [{"WebApiToken": []}]
        assert schema["paths"]["/health"]["get"]["security"] == [
            {"OperatorApiToken": []},
            {"WebApiToken": []},
        ]
    finally:
        await client.aclose()
        await database.dispose()


async def test_web_only_openapi_hides_disabled_operator_contract(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/web-only.db"
    await upgrade_database(database_url)
    database = Database(database_url)
    service = TicketService(database)
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        web_api_enabled=True,
        api_admin_token=SecretStr(OPERATOR_TOKEN),
        web_api_token=SecretStr(WEB_TOKEN),
        data_dir=tmp_path,
    )
    app = create_app(database=database, ticket_service=service, settings=settings)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        response = await client.get("/openapi.json", headers={"X-API-Token": WEB_TOKEN})
        assert response.status_code == 200
        assert all(not path.startswith("/api/v1/tickets") for path in response.json()["paths"])
        assert set(response.json()["components"]["securitySchemes"]) == {"WebApiToken"}
    finally:
        await client.aclose()
        await database.dispose()


async def test_concurrent_web_idempotency_has_one_durable_result(tmp_path: Path) -> None:
    client, database, _service = await _context(tmp_path)
    try:
        headers = {"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-concurrent-key"}
        payload = {
            "external_user_id": "concurrent-user",
            "email": "concurrent@example.com",
            "text": "same payload",
        }
        same = await asyncio.gather(
            client.post("/api/v1/web/messages", headers=headers, json=payload),
            client.post("/api/v1/web/messages", headers=headers, json=payload),
        )
        assert [response.status_code for response in same] == [200, 200]
        assert same[0].json() == same[1].json()

        conflict_headers = {
            "X-API-Token": WEB_TOKEN,
            "X-Idempotency-Key": "web-concurrent-conflict",
        }
        conflict = await asyncio.gather(
            client.post(
                "/api/v1/web/messages",
                headers=conflict_headers,
                json={**payload, "text": "payload A"},
            ),
            client.post(
                "/api/v1/web/messages",
                headers=conflict_headers,
                json={**payload, "text": "payload B"},
            ),
        )
        assert sorted(response.status_code for response in conflict) == [200, 409]
    finally:
        await client.aclose()
        await database.dispose()


async def test_web_photo_is_validated_persisted_and_downloadable(tmp_path: Path) -> None:
    client, database, _service = await _context(tmp_path)
    try:
        created = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-photo-0001"},
            data={
                "external_user_id": "photo-user",
                "email": "photo@example.com",
                "text": "Screenshot",
            },
            files={"photo": ("screen.png", b"\x89PNG\r\n\x1a\nimage-data", "image/png")},
        )
        assert created.status_code == 200
        ticket_id = created.json()["conversation"]["id"]
        page = await client.get(
            f"/api/v1/web/conversations/{ticket_id}/messages",
            headers={"X-API-Token": WEB_TOKEN},
        )
        media_url = page.json()["items"][0]["media_url"]
        assert media_url.startswith("/api/v1/web/media/")

        downloaded = await client.get(media_url, headers={"X-API-Token": WEB_TOKEN})
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "image/png"
        assert downloaded.content == b"\x89PNG\r\n\x1a\nimage-data"

        long_caption = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-photo-caption"},
            data={
                "external_user_id": "photo-user",
                "email": "photo@example.com",
                "text": "x" * 1025,
            },
            files={"photo": ("screen.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
        )
        assert long_caption.status_code == 422

        rejected = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-photo-0002"},
            data={"external_user_id": "photo-user", "email": "photo@example.com"},
            files={"photo": ("fake.png", b"not-an-image", "image/png")},
        )
        assert rejected.status_code == 422
        assert list((tmp_path / "web-media" / "tmp").iterdir()) == []

        unknown = await client.post(
            "/api/v1/web/messages",
            headers={"X-API-Token": WEB_TOKEN, "X-Idempotency-Key": "web-photo-0003"},
            data={
                "external_user_id": "photo-user",
                "email": "photo@example.com",
                "unknown": "not accepted",
            },
            files={"photo": ("screen.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
        )
        assert unknown.status_code == 422
    finally:
        await client.aclose()
        await database.dispose()


async def test_web_request_size_is_limited_without_content_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, database, _service = await _context(tmp_path)
    monkeypatch.setattr(web_api_routes, "MAX_WEB_MESSAGE_REQUEST_BYTES", 128)

    async def oversized_body() -> AsyncIterator[bytes]:
        yield b'{"external_user_id":"large","email":"large@example.com","text":"'
        yield b"x" * 200
        yield b'"}'

    try:
        response = await client.post(
            "/api/v1/web/messages",
            headers={
                "Content-Type": "application/json",
                "X-API-Token": WEB_TOKEN,
                "X-Idempotency-Key": "web-oversized-stream",
            },
            content=oversized_body(),
        )
        assert response.status_code == 413
    finally:
        await client.aclose()
        await database.dispose()


@pytest.mark.parametrize("mode", ["external_id", "email"])
async def test_web_identity_mode_is_persisted(tmp_path: Path, mode: str) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/{mode}.db"
    await upgrade_database(database_url)
    database = Database(database_url)
    service = TicketService(database)
    try:
        from suppsystem.api_idempotency import api_idempotency_command

        await service.accept_message(
            identity_mode=mode,
            external_user_id="stable-id" if mode == "external_id" else None,
            email="identity@example.com",
            display_name=None,
            remnawave_user_uuid=None,
            content="message",
            media=None,
            target_chat_id=-100123,
            command=api_idempotency_command(
                operation="web_message",
                resource=mode,
                key=f"identity-{mode}",
                payload={"mode": mode},
            ),
        )
        await service.validate_web_identity_mode(mode)
        with pytest.raises(RuntimeError, match="cannot change"):
            alternate_mode = "email" if mode == "external_id" else "external_id"
            await service.validate_web_identity_mode(alternate_mode)
    finally:
        await database.dispose()


async def test_successful_web_polling_is_not_written_to_audit_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, database, _service = await _context(tmp_path)
    try:
        created = await client.post(
            "/api/v1/web/messages",
            headers={
                "X-API-Token": WEB_TOKEN,
                "X-Idempotency-Key": "polling-log-create",
            },
            json={
                "external_user_id": "polling-log-user",
                "email": "polling-log@example.com",
                "text": "Message",
            },
        )
        ticket_id = created.json()["conversation_id"]
        caplog.clear()
        caplog.set_level(logging.INFO, logger="suppsystem.audit")

        conversation = await client.get(
            f"/api/v1/web/conversations/{ticket_id}",
            headers={"X-API-Token": WEB_TOKEN},
        )
        messages = await client.get(
            f"/api/v1/web/conversations/{ticket_id}/messages",
            headers={"X-API-Token": WEB_TOKEN},
        )
        assert conversation.status_code == 200
        assert messages.status_code == 200
        assert not [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "api_request_completed"
        ]

        invalid = await client.get(
            f"/api/v1/web/conversations/{ticket_id}/messages?after=invalid00",
            headers={"X-API-Token": WEB_TOKEN},
        )
        assert invalid.status_code == 422
        assert any(
            getattr(record, "event", None) == "api_request_completed"
            and getattr(record, "http_status", None) == 422
            for record in caplog.records
        )
    finally:
        await client.aclose()
        await database.dispose()
