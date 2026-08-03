from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select, text

from suppsystem.api import API_TICKET_CLOSED_TEXT, client_key_from_request, create_app
from suppsystem.config import Settings
from suppsystem.database import Database
from suppsystem.metrics import MetricsRegistry
from suppsystem.models import (
    DeliveryOutbox,
    Direction,
    OperatorAction,
    ReconciliationOutbox,
    TicketMessage,
    TicketStatus,
    WorkStatus,
)
from suppsystem.runtime_defaults import API_AUTH_FAILURE_LIMIT
from suppsystem.runtime_health import RuntimeHealth
from suppsystem.services import TicketService, TicketView

API_TOKEN = "0123456789abcdef0123456789abcdef"
WEB_RATE_TOKEN = "abcdef0123456789abcdef0123456789"


@pytest.fixture
async def api_context(
    tmp_path: Path,
) -> AsyncIterator[tuple[Any, Database, TicketService, TicketView]]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/support.db")
    await database.create_schema_for_tests()
    ticket_service = TicketService(database)
    ticket = await ticket_service.open_or_reopen(
        telegram_user_id=2001, display_name="API User", username="apiuser"
    )
    token = await ticket_service.claim_topic_provisioning(ticket.id)
    assert token is not None
    ticket = await ticket_service.attach_topic(ticket.id, 9001, token=token)
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
    )
    app = create_app(database=database, ticket_service=ticket_service, settings=settings)
    yield app, database, ticket_service, ticket
    await database.dispose()


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _api_message_counts(database: Database, ticket_id: str) -> tuple[int, int, int, int]:
    message_key_prefix = f"api:message:{ticket_id}:%"
    reopen_key_prefix = f"api:message-reopen:{ticket_id}:%"
    async with database.session() as session:
        messages = await session.scalar(
            select(func.count())
            .select_from(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id, TicketMessage.channel == "api")
        )
        deliveries = await session.scalar(
            select(func.count())
            .select_from(DeliveryOutbox)
            .where(
                DeliveryOutbox.ticket_id == ticket_id,
                DeliveryOutbox.idempotency_key.like(message_key_prefix),
            )
        )
        send_actions = await session.scalar(
            select(func.count())
            .select_from(OperatorAction)
            .where(
                OperatorAction.ticket_id == ticket_id,
                OperatorAction.action == "send_ticket_message",
                OperatorAction.idempotency_key.like(message_key_prefix),
            )
        )
        reopen_actions = await session.scalar(
            select(func.count())
            .select_from(OperatorAction)
            .where(
                OperatorAction.ticket_id == ticket_id,
                OperatorAction.action == "reopen_ticket",
                OperatorAction.idempotency_key.like(reopen_key_prefix),
            )
        )
    return tuple(int(value or 0) for value in (messages, deliveries, send_actions, reopen_actions))


async def test_api_messages_support_limit_and_offset(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, ticket_service, ticket = api_context
    for index in range(3):
        await ticket_service.enqueue_text(
            ticket_id=ticket.id,
            direction=Direction.OPERATOR_TO_USER,
            text=f"reply {index}",
            target_chat_id=ticket.telegram_user_id,
            idempotency_key=f"api:test:{index}",
        )
    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/tickets/{ticket.id}/messages",
            params={"limit": 1, "offset": 1},
            headers={"X-API-Token": API_TOKEN},
        )
    payload = response.json()

    assert response.status_code == 200
    assert len(payload) == 1
    assert payload[0]["content"] == "reply 1"


async def test_api_close_can_notify_user_via_outbox(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, ticket_service, ticket = api_context
    headers = {"X-API-Token": API_TOKEN, "X-Idempotency-Key": "close-one"}
    async with _client(app) as client:
        response = await client.post(
            f"/api/v1/tickets/{ticket.id}/close",
            headers=headers,
            json={"notify_user": True},
        )
        duplicate = await client.post(
            f"/api/v1/tickets/{ticket.id}/close",
            headers=headers,
            json={"notify_user": True},
        )
    jobs = await ticket_service.outbox.claim_due_deliveries()

    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert duplicate.json()["changed"] is True
    assert len(jobs) == 1
    assert jobs[0].payload == {
        "kind": "send_text",
        "target_chat_id": ticket.telegram_user_id,
        "text": API_TICKET_CLOSED_TEXT,
    }


async def test_api_message_failure_rolls_back_and_surfaces_retryable_error(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, ticket_service, ticket = api_context
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="atomic-message-rollback-close",
    )
    delivery_key = f"api:message:{ticket.id}:atomic-rollback"
    async with database.session() as session:
        await session.execute(
            text(
                f"""
                CREATE TRIGGER fail_atomic_api_delivery
                BEFORE INSERT ON delivery_outbox
                WHEN NEW.idempotency_key = '{delivery_key}'
                BEGIN
                    SELECT RAISE(ABORT, 'forced atomic API failure');
                END
                """
            )
        )
        await session.commit()

    async with _client(app) as client:
        response = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers={
                "X-API-Token": API_TOKEN,
                "X-Idempotency-Key": "atomic-rollback",
            },
            json={"text": "must roll back"},
        )

    current = await ticket_service.get_ticket(ticket.id)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert current.status is TicketStatus.CLOSED
    assert await _api_message_counts(database, ticket.id) == (0, 0, 0, 0)


async def test_duplicate_api_message_after_reclose_does_not_reopen_ticket(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, ticket_service, ticket = api_context
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="atomic-duplicate-first-close",
    )
    headers = {
        "X-API-Token": API_TOKEN,
        "X-Idempotency-Key": "atomic-duplicate",
    }
    async with _client(app) as client:
        first = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers=headers,
            json={"text": "deliver exactly once"},
        )
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="atomic-duplicate-second-close",
    )
    async with _client(app) as client:
        duplicate = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers=headers,
            json={"text": "deliver exactly once"},
        )

    current = await ticket_service.get_ticket(ticket.id)
    assert first.json()["changed"] is True
    assert duplicate.json()["changed"] is True
    assert current.status is TicketStatus.CLOSED
    assert await _api_message_counts(database, ticket.id) == (1, 1, 1, 1)


async def test_api_message_rejects_same_key_with_different_payload(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, _ticket_service, ticket = api_context
    headers = {
        "X-API-Token": API_TOKEN,
        "X-Idempotency-Key": "message-payload-conflict",
    }

    async with _client(app) as client:
        first = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers=headers,
            json={"text": "original payload"},
        )
        conflict = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers=headers,
            json={"text": "different payload"},
        )

    assert first.status_code == 200
    assert first.json() == {"changed": True}
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert await _api_message_counts(database, ticket.id) == (1, 1, 1, 0)


async def test_api_message_duplicate_guard_rechecks_payload_fingerprint(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, ticket_service, ticket = api_context
    headers = {
        "X-API-Token": API_TOKEN,
        "X-Idempotency-Key": "stale-replay-conflict",
    }
    async with _client(app) as client:
        first = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers=headers,
            json={"text": "original payload"},
        )

        original_replay = ticket_service._operator_message_replay
        replay_calls = 0

        async def stale_initial_replays(session: Any, command: Any) -> Any:
            nonlocal replay_calls
            replay_calls += 1
            if replay_calls <= 2:
                return None
            return await original_replay(session, command)

        ticket_service._operator_message_replay = stale_initial_replays  # type: ignore[method-assign]
        try:
            conflict = await client.post(
                f"/api/v1/tickets/{ticket.id}/messages",
                headers=headers,
                json={"text": "different payload"},
            )
        finally:
            ticket_service._operator_message_replay = original_replay  # type: ignore[method-assign]

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert replay_calls == 3


async def test_api_close_rejects_same_key_with_different_notification_payload(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, ticket_service, ticket = api_context
    headers = {
        "X-API-Token": API_TOKEN,
        "X-Idempotency-Key": "close-payload-conflict",
    }

    async with _client(app) as client:
        first = await client.post(
            f"/api/v1/tickets/{ticket.id}/close",
            headers=headers,
            json={"notify_user": False},
        )
        conflict = await client.post(
            f"/api/v1/tickets/{ticket.id}/close",
            headers=headers,
            json={"notify_user": True},
        )

    assert first.json() == {"changed": True}
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert await ticket_service.outbox.claim_due_deliveries() == []


async def test_api_noop_result_is_durably_replayed(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, ticket_service, ticket = api_context
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="close-before-api-noop",
    )
    headers = {
        "X-API-Token": API_TOKEN,
        "X-Idempotency-Key": "durable-noop-close",
    }

    async with _client(app) as client:
        first = await client.post(
            f"/api/v1/tickets/{ticket.id}/close",
            headers=headers,
            json={"notify_user": False},
        )
    await ticket_service.reopen(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="reopen-after-api-noop",
    )
    async with _client(app) as client:
        replay = await client.post(
            f"/api/v1/tickets/{ticket.id}/close",
            headers=headers,
            json={"notify_user": False},
        )

    assert first.json() == {"changed": False}
    assert replay.json() == {"changed": False}
    assert (await ticket_service.get_ticket(ticket.id)).status is TicketStatus.OPEN


async def test_concurrent_same_key_api_messages_commit_one_atomic_command(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, ticket_service, ticket = api_context
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="atomic-same-key-close",
    )
    headers = {
        "X-API-Token": API_TOKEN,
        "X-Idempotency-Key": "atomic-same-key",
    }
    async with _client(app) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/tickets/{ticket.id}/messages",
                    headers=headers,
                    json={"text": "one command"},
                )
                for _ in range(2)
            )
        )

    current = await ticket_service.get_ticket(ticket.id)
    assert [response.json()["changed"] for response in responses] == [True, True]
    assert current.status is TicketStatus.OPEN
    assert await _api_message_counts(database, ticket.id) == (1, 1, 1, 1)


async def test_concurrent_same_key_different_payload_has_one_winner(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, _ticket_service, ticket = api_context
    headers = {
        "X-API-Token": API_TOKEN,
        "X-Idempotency-Key": "concurrent-payload-conflict",
    }

    async with _client(app) as client:
        responses = await asyncio.gather(
            client.post(
                f"/api/v1/tickets/{ticket.id}/messages",
                headers=headers,
                json={"text": "payload a"},
            ),
            client.post(
                f"/api/v1/tickets/{ticket.id}/messages",
                headers=headers,
                json={"text": "payload b"},
            ),
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert await _api_message_counts(database, ticket.id) == (1, 1, 1, 0)


async def test_concurrent_distinct_api_messages_reopen_once_and_commit_both(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, ticket_service, ticket = api_context
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="atomic-distinct-close",
    )

    async with _client(app) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/tickets/{ticket.id}/messages",
                    headers={
                        "X-API-Token": API_TOKEN,
                        "X-Idempotency-Key": key,
                    },
                    json={"text": f"message {key}"},
                )
                for key in ("atomic-distinct-a", "atomic-distinct-b")
            )
        )

    current = await ticket_service.get_ticket(ticket.id)
    assert [response.json()["changed"] for response in responses] == [True, True]
    assert current.status is TicketStatus.OPEN
    assert await _api_message_counts(database, ticket.id) == (2, 2, 2, 1)


async def test_api_message_queues_topic_reconciliation_after_atomic_commit(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, ticket = api_context
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="atomic-sync-close",
    )
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
    )
    app = create_app(database=database, ticket_service=ticket_service, settings=settings)
    async with _client(app) as client:
        response = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers={
                "X-API-Token": API_TOKEN,
                "X-Idempotency-Key": "atomic-sync-message",
            },
            json={"text": "observe committed state"},
        )

    assert response.json()["changed"] is True
    async with database.session() as session:
        job = await session.scalar(
            select(ReconciliationOutbox).where(
                ReconciliationOutbox.ticket_id == ticket.id,
                ReconciliationOutbox.kind == "telegram_topic",
            )
        )
        action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == f"api:message:{ticket.id}:atomic-sync-message"
            )
        )
    assert job is not None
    assert WorkStatus(job.status) is WorkStatus.PENDING
    assert job.payload == {"desired_status": TicketStatus.OPEN.value}
    assert action is not None
    assert action.payload["api_idempotency"]["response"] == {
        "changed": True,
        "reopened": True,
        "ticket_id": ticket.id,
    }


async def test_blocked_closed_ticket_is_not_reopened_by_api_message(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, ticket_service, ticket = api_context
    await ticket_service.close(
        ticket_id=ticket.id,
        operator_telegram_id=999,
        idempotency_key="atomic-blocked-close",
    )
    await ticket_service.block(
        telegram_user_id=ticket.telegram_user_id,
        operator_telegram_id=999,
        ticket_id=ticket.id,
        idempotency_key="atomic-block-user",
    )

    async with _client(app) as client:
        response = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers={
                "X-API-Token": API_TOKEN,
                "X-Idempotency-Key": "atomic-blocked-message",
            },
            json={"text": "must stay blocked"},
        )

    current = await ticket_service.get_ticket(ticket.id)
    assert response.json()["changed"] is False
    assert current.status is TicketStatus.CLOSED
    assert await _api_message_counts(database, ticket.id) == (0, 0, 1, 0)


async def test_api_ticket_detail_does_not_embed_messages(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, ticket_service, ticket = api_context
    await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="hidden from detail",
        target_chat_id=ticket.telegram_user_id,
        idempotency_key="api:detail:hidden",
    )
    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/tickets/{ticket.id}",
            headers={"X-API-Token": API_TOKEN},
        )
    payload = response.json()

    assert response.status_code == 200
    assert payload["id"] == ticket.id
    assert payload["last_activity_at"] is not None
    assert "messages" not in payload


def test_api_routes_include_auth_dependency_for_docs_openapi_and_health(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, _ticket_service, _ticket = api_context
    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}

    for path in ("/health", "/ready", "/metrics", "/docs", "/openapi.json"):
        assert path in routes
        assert routes[path].dependant.dependencies


async def test_api_auth_dependency_is_noop_when_unsafe_auth_disabled(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, _ticket = api_context
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_unsafe_disable_auth=True,
    )
    app = create_app(database=database, ticket_service=ticket_service, settings=settings)
    async with _client(app) as client:
        response = await client.get("/health")

    assert response.status_code == 200


async def test_metrics_exposes_queue_and_runtime_metrics_without_pii(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, ticket_service, ticket = api_context
    await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="must not appear in metrics",
        target_chat_id=ticket.telegram_user_id,
        idempotency_key="metrics-delivery-1",
    )

    async with _client(app) as client:
        response = await client.get("/metrics", headers={"X-API-Token": API_TOKEN})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'suppsystem_queue_depth{queue="delivery"} 1' in response.text
    assert "suppsystem_queue_oldest_age_seconds" in response.text
    assert "suppsystem_heartbeat_age_seconds" in response.text
    assert "must not appear" not in response.text
    assert str(ticket.telegram_user_id) not in response.text


async def test_metrics_use_retained_attempt_gauge_and_record_remnawave_failures(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, _ticket_service, _ticket = api_context
    metrics = MetricsRegistry()
    metrics.observe_request("remnawave", "http_5xx", 0.25)
    metrics.observe_request("remnawave", "request_error", 0.5)
    health = RuntimeHealth()
    health.register("database")
    health.ready("database")

    rendered = await metrics.render(database, health)

    assert "# TYPE suppsystem_retained_job_attempts gauge" in rendered
    assert "suppsystem_job_attempts_total" not in rendered
    assert 'suppsystem_events_total{component="remnawave",outcome="http_5xx"} 1' in rendered
    assert 'suppsystem_events_total{component="remnawave",outcome="request_error"} 1' in rendered


async def test_ready_reports_runtime_components(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, _ticket = api_context
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
    )
    health = RuntimeHealth()
    health.register("database")
    health.register("api")
    health.register("panel", configured=False)
    health.register("delivery_worker", progress_timeout_seconds=45)
    health.ready("api")
    health.progress("delivery_worker")
    app = create_app(
        database=database,
        ticket_service=ticket_service,
        settings=settings,
        runtime_health=health,
    )

    async with _client(app) as client:
        response = await client.get("/ready", headers={"X-API-Token": API_TOKEN})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "components": {
            "database": "ready",
            "api": "ready",
            "panel": "not_configured",
            "delivery_worker": "ready",
        },
    }


async def test_ready_returns_503_for_stale_enabled_worker(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, _ticket = api_context
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
    )
    health = RuntimeHealth()
    health.register("database")
    health.register("api")
    health.register("delivery_worker", progress_timeout_seconds=0)
    health.ready("api")
    health.progress("delivery_worker", now=0)
    app = create_app(
        database=database,
        ticket_service=ticket_service,
        settings=settings,
        runtime_health=health,
    )

    async with _client(app) as client:
        response = await client.get("/ready", headers={"X-API-Token": API_TOKEN})

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "components": {
            "database": "ready",
            "api": "ready",
            "delivery_worker": "degraded",
        },
    }


async def test_ready_returns_safe_503_when_database_probe_fails(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, _database, ticket_service, _ticket = api_context

    class FailedSession:
        async def __aenter__(self) -> None:
            raise RuntimeError("postgresql://secret@private.example/support")

        async def __aexit__(self, *args: object) -> None:
            return None

    class FailedDatabase:
        def session(self) -> FailedSession:
            return FailedSession()

    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
    )
    health = RuntimeHealth()
    health.register("database")
    health.register("api")
    health.ready("api")
    app = create_app(
        database=FailedDatabase(),  # type: ignore[arg-type]
        ticket_service=ticket_service,
        settings=settings,
        runtime_health=health,
    )

    async with _client(app) as client:
        response = await client.get("/ready", headers={"X-API-Token": API_TOKEN})

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "components": {"database": "degraded", "api": "ready"},
    }
    assert "secret" not in response.text


async def test_api_http_trace_is_returned_and_persisted_on_action(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, database, _ticket_service, ticket = api_context

    async with _client(app) as client:
        response = await client.post(
            f"/api/v1/tickets/{ticket.id}/close",
            headers={
                "X-API-Token": API_TOKEN,
                "X-Idempotency-Key": "http-trace-close-1",
            },
            json={"notify_user": False},
        )

    trace_id = response.headers["X-Trace-ID"]
    async with database.session() as session:
        action = await session.scalar(
            select(OperatorAction).where(
                OperatorAction.idempotency_key == f"api:close:{ticket.id}:http-trace-close-1"
            )
        )

    assert response.status_code == 200
    assert len(trace_id) == 32
    assert action is not None
    assert action.trace_id == trace_id


@pytest.mark.parametrize(
    ("headers", "payload"),
    [
        (
            {"X-API-Token": API_TOKEN, "X-Idempotency-Key": "bad key"},
            {"text": "valid"},
        ),
        (
            {"X-API-Token": API_TOKEN, "X-Idempotency-Key": "valid-key-1"},
            {"text": "   "},
        ),
        (
            {"X-API-Token": API_TOKEN, "X-Idempotency-Key": "valid-key-2"},
            {"text": "valid", "unexpected": "field"},
        ),
    ],
)
async def test_api_validation_uses_safe_error_envelope(
    api_context: tuple[Any, Database, TicketService, TicketView],
    headers: dict[str, str],
    payload: dict[str, str],
) -> None:
    app, _database, _ticket_service, ticket = api_context

    async with _client(app) as client:
        response = await client.post(
            f"/api/v1/tickets/{ticket.id}/messages",
            headers=headers,
            json=payload,
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "Request validation failed",
            "trace_id": response.headers["X-Trace-ID"],
        }
    }


async def test_api_pagination_offset_has_upper_bound(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, _ticket_service, _ticket = api_context

    async with _client(app) as client:
        response = await client.get(
            "/api/v1/tickets?offset=100001",
            headers={"X-API-Token": API_TOKEN},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_api_rate_limit_returns_retry_after(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, _ticket = api_context
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
        api_requests_per_minute=2,
    )
    app = create_app(database=database, ticket_service=ticket_service, settings=settings)

    async with _client(app) as client:
        responses = [
            await client.get("/health", headers={"X-API-Token": API_TOKEN}) for _ in range(3)
        ]

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1].headers["Retry-After"] == "60"
    assert responses[-1].json()["error"]["code"] == "rate_limited"


async def test_api_rate_limits_are_independent_per_configured_token(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, _ticket = api_context
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
        web_api_enabled=True,
        web_api_token=SecretStr(WEB_RATE_TOKEN),
        api_requests_per_minute=2,
    )
    app = create_app(database=database, ticket_service=ticket_service, settings=settings)

    async with _client(app) as client:
        operator_responses = [
            await client.get("/health", headers={"X-API-Token": API_TOKEN}) for _ in range(3)
        ]
        web_response = await client.get("/health", headers={"X-API-Token": WEB_RATE_TOKEN})

    assert [response.status_code for response in operator_responses] == [200, 200, 429]
    assert web_response.status_code == 200


def test_api_trusted_proxy_walks_forwarded_chain_from_nearest_hop() -> None:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [
                (
                    b"x-forwarded-for",
                    b"203.0.113.99, 198.51.100.7, 10.0.0.20",
                )
            ],
            "client": ("10.0.0.10", 12345),
            "server": ("test", 80),
        }
    )

    assert client_key_from_request(request, frozenset({"10.0.0.0/24"})) == "198.51.100.7"


def test_nginx_example_overwrites_untrusted_forwarded_chain() -> None:
    config = (
        Path(__file__).resolve().parents[1] / "deploy" / "nginx" / "suppsystem-api.conf.example"
    ).read_text(encoding="utf-8")

    assert "proxy_set_header X-Forwarded-For $remote_addr;" in config
    assert "$proxy_add_x_forwarded_for" not in config

    assert "client_max_body_size 11m;" in config


async def test_api_auth_failures_are_rate_limited(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, _ticket = api_context
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        api_enabled=True,
        api_admin_token=SecretStr(API_TOKEN),
        api_requests_per_minute=100,
    )
    app = create_app(database=database, ticket_service=ticket_service, settings=settings)

    async with _client(app) as client:
        responses = [
            await client.get("/health", headers={"X-API-Token": "wrong-token"})
            for _ in range(API_AUTH_FAILURE_LIMIT + 1)
        ]

    assert [response.status_code for response in responses] == [401] * API_AUTH_FAILURE_LIMIT + [
        429
    ]
    assert responses[-1].json()["error"]["code"] == "rate_limited"


async def test_api_not_found_does_not_expose_internal_detail(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    app, _database, _ticket_service, _ticket = api_context
    missing_id = "00000000-0000-4000-8000-000000000000"

    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/tickets/{missing_id}",
            headers={"X-API-Token": API_TOKEN},
        )

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "not_found",
        "message": "Resource not found",
        "trace_id": response.headers["X-Trace-ID"],
    }


async def test_metrics_cache_database_aggregates(
    api_context: tuple[Any, Database, TicketService, TicketView],
) -> None:
    _app, database, ticket_service, ticket = api_context

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    metrics = MetricsRegistry(database_cache_ttl_seconds=30, monotonic=clock)
    health = RuntimeHealth()
    health.register("database")
    health.ready("database")

    before = await metrics.render(database, health)
    assert 'suppsystem_queue_depth{queue="delivery"} 0' in before
    await ticket_service.enqueue_text(
        ticket_id=ticket.id,
        direction=Direction.OPERATOR_TO_USER,
        text="queued after scrape",
        target_chat_id=ticket.telegram_user_id,
        idempotency_key="metrics-cache-delivery",
    )
    cached = await metrics.render(database, health)
    assert 'suppsystem_queue_depth{queue="delivery"} 0' in cached
    clock.value = 31.0
    refreshed = await metrics.render(database, health)
    assert 'suppsystem_queue_depth{queue="delivery"} 1' in refreshed
