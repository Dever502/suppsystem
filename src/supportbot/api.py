from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from ipaddress import ip_address, ip_network

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from supportbot.api_idempotency import ApiIdempotencyConflictError
from supportbot.api_routes import API_TICKET_CLOSED_TEXT as API_TICKET_CLOSED_TEXT
from supportbot.api_routes import register_routes
from supportbot.api_schemas import (
    IDEMPOTENCY_KEY_PATTERN as IDEMPOTENCY_KEY_PATTERN,
)
from supportbot.api_schemas import (
    TICKET_ID_PATTERN as TICKET_ID_PATTERN,
)
from supportbot.api_schemas import CloseTicketRequest as CloseTicketRequest
from supportbot.api_schemas import IdempotencyKey as IdempotencyKey
from supportbot.api_schemas import MessageResponse as MessageResponse
from supportbot.api_schemas import MutationResponse as MutationResponse
from supportbot.api_schemas import SendMessageRequest as SendMessageRequest
from supportbot.api_schemas import StrictRequest as StrictRequest
from supportbot.api_schemas import TicketId as TicketId
from supportbot.api_schemas import TicketResponse as TicketResponse
from supportbot.api_security import InMemoryRateLimiter
from supportbot.audit import record_event
from supportbot.config import Settings
from supportbot.database import Database
from supportbot.metrics import MetricsRegistry
from supportbot.runtime_health import RuntimeHealth
from supportbot.service_types import TicketView
from supportbot.services import TicketService
from supportbot.trace import trace_id_var
from supportbot.version import PROJECT_VERSION

logger = logging.getLogger(__name__)
MAX_FORWARDED_HOPS = 32


def _valid_ip_text(value: str) -> str | None:
    try:
        return str(ip_address(value.strip()))
    except ValueError:
        return None


def _is_trusted_proxy(host: str, trusted_proxy_ips: frozenset[str]) -> bool:
    if not trusted_proxy_ips:
        return False
    try:
        proxy_ip = ip_address(host)
    except ValueError:
        return False
    return any(proxy_ip in ip_network(candidate, strict=False) for candidate in trusted_proxy_ips)


def client_key_from_request(request: Request, trusted_proxy_ips: frozenset[str]) -> str:
    direct_host = request.client.host if request.client is not None else "unknown"
    if not _is_trusted_proxy(direct_host, trusted_proxy_ips):
        return direct_host

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        raw_hops = forwarded_for.split(",")
        if not 1 <= len(raw_hops) <= MAX_FORWARDED_HOPS:
            return direct_host
        forwarded_hops = [_valid_ip_text(hop) for hop in raw_hops]
        if any(hop is None for hop in forwarded_hops):
            return direct_host
        for forwarded_host in reversed(forwarded_hops):
            assert forwarded_host is not None
            if not _is_trusted_proxy(forwarded_host, trusted_proxy_ips):
                return forwarded_host
        return direct_host

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        real_host = _valid_ip_text(real_ip)
        if real_host is not None:
            return real_host

    return direct_host


def create_app(
    *,
    database: Database,
    ticket_service: TicketService,
    settings: Settings,
    sync_ticket_topic: Callable[[TicketView], Awaitable[bool]] | None = None,
    runtime_health: RuntimeHealth | None = None,
    metrics: MetricsRegistry | None = None,
) -> FastAPI:
    if runtime_health is None:
        runtime_health = RuntimeHealth()
        runtime_health.register("database")
        runtime_health.register("api")
        runtime_health.ready("api")
    if metrics is None:
        metrics = MetricsRegistry()
    request_limiter = InMemoryRateLimiter(
        limit=settings.api_rate_limit_requests,
        window_seconds=settings.api_rate_limit_window_seconds,
    )
    auth_failure_limiter = InMemoryRateLimiter(
        limit=settings.api_auth_failure_limit,
        window_seconds=settings.api_auth_failure_window_seconds,
    )

    def client_key(request: Request) -> str:
        return client_key_from_request(request, settings.api_trusted_proxy_ips)

    async def require_operator_token(
        request: Request,
        x_api_token: str | None = Header(default=None, alias="X-API-Token"),
    ) -> None:
        if settings.api_unsafe_disable_auth:
            return
        configured_token = settings.api_admin_token
        key = f"auth:{client_key(request)}"
        authenticated = (
            configured_token is not None
            and x_api_token is not None
            and secrets.compare_digest(x_api_token, configured_token.get_secret_value())
        )
        if not authenticated:
            allowed, retry_after = await auth_failure_limiter.consume(key)
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many authentication attempts",
                    headers={"Retry-After": str(retry_after)},
                )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        await auth_failure_limiter.reset(key)

    app = FastAPI(
        title="Telegram Support Platform API",
        version=PROJECT_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(require_operator_token)],
    )
    app.router.add_event_handler("startup", lambda: runtime_health.ready("api"))

    def error_response(
        *,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        trace_id = trace_id_var.get()
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "trace_id": trace_id,
                }
            },
            headers=headers,
        )

    @app.middleware("http")
    async def trace_audit_and_rate_limit(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = uuid.uuid4().hex
        trace_token = trace_id_var.set(trace_id)
        started_at = time.monotonic()
        response: Response
        try:
            allowed, retry_after = await request_limiter.consume(f"request:{client_key(request)}")
            if not allowed:
                response = error_response(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code="rate_limited",
                    message="Too many requests",
                    headers={"Retry-After": str(retry_after)},
                )
            else:
                try:
                    response = await call_next(request)
                except Exception:
                    logger.exception(
                        "Unhandled API request error",
                        extra={
                            "event": "api_request_unhandled_error",
                            "http_method": request.method,
                            "http_path": request.url.path,
                        },
                    )
                    record_event(
                        "api_request_unhandled_error",
                        http_method=request.method,
                        http_path=request.url.path,
                    )
                    response = error_response(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        code="internal_error",
                        message="Internal server error",
                    )
            response.headers["X-Trace-ID"] = trace_id
            duration_ms = round((time.monotonic() - started_at) * 1000, 2)
            record_event(
                "api_request_completed",
                http_method=request.method,
                http_path=request.url.path,
                http_status=response.status_code,
                duration_ms=duration_ms,
            )
            return response
        finally:
            trace_id_var.reset(trace_token)

    safe_http_errors = {
        status.HTTP_400_BAD_REQUEST: ("invalid_request", "Invalid request"),
        status.HTTP_401_UNAUTHORIZED: ("unauthorized", "Unauthorized"),
        status.HTTP_403_FORBIDDEN: ("forbidden", "Forbidden"),
        status.HTTP_404_NOT_FOUND: ("not_found", "Resource not found"),
        status.HTTP_409_CONFLICT: ("conflict", "Request conflict"),
        status.HTTP_429_TOO_MANY_REQUESTS: ("rate_limited", "Too many requests"),
        status.HTTP_503_SERVICE_UNAVAILABLE: (
            "service_unavailable",
            "Service unavailable",
        ),
    }

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, error: HTTPException) -> JSONResponse:
        del request
        code, message = safe_http_errors.get(
            error.status_code,
            ("request_failed", "Request failed"),
        )
        return error_response(
            status_code=error.status_code,
            code=code,
            message=message,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del request, error
        return error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="validation_error",
            message="Request validation failed",
        )

    @app.exception_handler(ApiIdempotencyConflictError)
    async def handle_idempotency_conflict(
        request: Request, error: ApiIdempotencyConflictError
    ) -> JSONResponse:
        del request, error
        return error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            message="Idempotency key conflicts with a previous request",
        )

    register_routes(
        app,
        database=database,
        ticket_service=ticket_service,
        settings=settings,
        runtime_health=runtime_health,
        metrics=metrics,
        sync_ticket_topic=sync_ticket_topic,
    )

    return app
