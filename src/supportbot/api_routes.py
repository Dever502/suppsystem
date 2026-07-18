from __future__ import annotations

from fastapi import FastAPI, Query, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select, text

from supportbot.api_idempotency import api_idempotency_command
from supportbot.api_schemas import (
    CloseTicketRequest,
    IdempotencyKey,
    MessageResponse,
    MutationResponse,
    SendMessageRequest,
    TicketId,
    TicketResponse,
    ticket_response,
)
from supportbot.config import Settings
from supportbot.database import Database
from supportbot.metrics import MetricsRegistry
from supportbot.models import Direction, TicketMessage, TicketStatus
from supportbot.runtime_health import RuntimeHealth
from supportbot.services import TicketService

API_TICKET_CLOSED_TEXT = (
    "Обращение закрыто оператором. Если вопрос остался, отправьте новое сообщение — "
    "мы откроем обращение снова."
)


async def read_messages(
    ticket_id: str, database: Database, *, limit: int, offset: int
) -> list[MessageResponse]:
    statement = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.created_at)
        .offset(offset)
        .limit(limit)
    )
    async with database.session() as session:
        messages = list((await session.scalars(statement)).all())
    return [
        MessageResponse(
            id=message.id,
            direction=Direction(message.direction),
            channel=message.channel,
            content=message.content,
            media=message.media,
            source_chat_id=message.source_chat_id,
            source_message_id=message.source_message_id,
            created_at=message.created_at,
        )
        for message in messages
    ]


def register_routes(
    app: FastAPI,
    *,
    database: Database,
    ticket_service: TicketService,
    settings: Settings,
    runtime_health: RuntimeHealth,
    metrics: MetricsRegistry,
) -> None:
    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["health"], response_model=None)
    async def ready() -> dict[str, object] | JSONResponse:
        try:
            async with database.session() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            runtime_health.degraded("database")
        else:
            runtime_health.ready("database")
        snapshot = runtime_health.snapshot()
        payload: dict[str, object] = {
            "status": "ready" if snapshot.ready else "degraded",
            "components": snapshot.components,
        }
        if not snapshot.ready:
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)
        return payload

    @app.get("/metrics", tags=["health"], response_class=PlainTextResponse)
    async def prometheus_metrics() -> str:
        return await metrics.render(database, runtime_health)

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="Telegram Support Platform API - Docs",
        )

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_schema() -> dict[str, object]:
        return get_openapi(title=app.title, version=app.version, routes=app.routes)

    @app.get("/api/v1/tickets", response_model=list[TicketResponse], tags=["tickets"])
    async def list_tickets(
        ticket_status: TicketStatus | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100_000),
    ) -> list[TicketResponse]:
        tickets = await ticket_service.list_tickets(
            status=ticket_status, limit=limit, offset=offset
        )
        return [ticket_response(ticket) for ticket in tickets]

    @app.get("/api/v1/tickets/{ticket_id}", response_model=TicketResponse, tags=["tickets"])
    async def get_ticket(ticket_id: TicketId) -> TicketResponse:
        return ticket_response(await ticket_service.get_ticket(ticket_id))

    @app.get(
        "/api/v1/tickets/{ticket_id}/messages",
        response_model=list[MessageResponse],
        tags=["messages"],
    )
    async def get_messages(
        ticket_id: TicketId,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100_000),
    ) -> list[MessageResponse]:
        await ticket_service.get_ticket(ticket_id)
        return await read_messages(ticket_id, database, limit=limit, offset=offset)

    @app.post(
        "/api/v1/tickets/{ticket_id}/messages",
        response_model=MutationResponse,
        tags=["messages"],
    )
    async def send_message(
        ticket_id: TicketId,
        request: SendMessageRequest,
        x_idempotency_key: IdempotencyKey,
    ) -> MutationResponse:
        command = api_idempotency_command(
            operation="message",
            resource=ticket_id,
            key=x_idempotency_key,
            payload={"text": request.text},
        )
        result = await ticket_service.send_operator_message(
            ticket_id=ticket_id,
            operator_telegram_id=settings.api_operator_telegram_id,
            text=request.text,
            idempotency_key=command.storage_key,
            reopen_idempotency_key=(f"api:message-reopen:{ticket_id}:{x_idempotency_key}"),
            api_idempotency=command,
        )
        return MutationResponse(changed=result.changed)

    @app.post(
        "/api/v1/tickets/{ticket_id}/close",
        response_model=MutationResponse,
        tags=["tickets"],
    )
    async def close_ticket(
        ticket_id: TicketId,
        x_idempotency_key: IdempotencyKey,
        request: CloseTicketRequest | None = None,
    ) -> MutationResponse:
        notify_user = request.notify_user if request is not None else False
        command = api_idempotency_command(
            operation="close",
            resource=ticket_id,
            key=x_idempotency_key,
            payload={"notify_user": notify_user},
        )
        ticket = await ticket_service.get_ticket(ticket_id)
        changed = await ticket_service.close(
            ticket_id=ticket_id,
            operator_telegram_id=settings.api_operator_telegram_id,
            idempotency_key=command.storage_key,
            notification_text=API_TICKET_CLOSED_TEXT if notify_user else None,
            notification_target_chat_id=ticket.telegram_user_id if notify_user else None,
            notification_idempotency_key=(
                f"api:close:{ticket_id}:{x_idempotency_key}:notification"
            ),
            api_idempotency=command,
        )
        return MutationResponse(changed=changed)

    @app.post(
        "/api/v1/tickets/{ticket_id}/reopen",
        response_model=MutationResponse,
        tags=["tickets"],
    )
    async def reopen_ticket(
        ticket_id: TicketId, x_idempotency_key: IdempotencyKey
    ) -> MutationResponse:
        command = api_idempotency_command(
            operation="reopen",
            resource=ticket_id,
            key=x_idempotency_key,
            payload={},
        )
        changed = await ticket_service.reopen(
            ticket_id=ticket_id,
            operator_telegram_id=settings.api_operator_telegram_id,
            idempotency_key=command.storage_key,
            api_idempotency=command,
        )
        return MutationResponse(changed=changed)
