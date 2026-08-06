from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from ipaddress import ip_address, ip_network
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from resolvate.api_idempotency import ApiIdempotencyConflictError
from resolvate.api_routes import API_TICKET_CLOSED_TEXT as API_TICKET_CLOSED_TEXT
from resolvate.api_routes import register_routes
from resolvate.api_security import InMemoryRateLimiter
from resolvate.audit import record_event
from resolvate.config import Settings
from resolvate.database import Database
from resolvate.media_storage import LocalMediaStorage
from resolvate.metrics import MetricsRegistry
from resolvate.runtime_defaults import (
    API_AUTH_FAILURE_LIMIT,
    API_AUTH_FAILURE_WINDOW_SECONDS,
    API_RATE_LIMIT_WINDOW_SECONDS,
)
from resolvate.runtime_health import RuntimeHealth
from resolvate.service_types import TicketNotFoundError
from resolvate.services import TicketService
from resolvate.trace import trace_id_var
from resolvate.user_message_limits import UserMessageRateLimiter
from resolvate.version import PROJECT_VERSION
from resolvate.web_api_routes import register_web_routes

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
    runtime_health: RuntimeHealth | None = None,
    metrics: MetricsRegistry | None = None,
    media_storage: LocalMediaStorage | None = None,
    user_message_limiter: UserMessageRateLimiter | None = None,
) -> FastAPI:
    if runtime_health is None:
        runtime_health = RuntimeHealth()
        runtime_health.register("database")
        runtime_health.register("api")
        runtime_health.ready("api")
    if metrics is None:
        metrics = MetricsRegistry()
    if media_storage is None:
        media_storage = LocalMediaStorage(settings.data_dir)
    if user_message_limiter is None:
        user_message_limiter = UserMessageRateLimiter(
            per_minute=settings.user_messages_per_minute,
            per_hour=settings.user_messages_per_hour,
        )

    request_limiter = InMemoryRateLimiter(
        limit=settings.api_requests_per_minute,
        window_seconds=API_RATE_LIMIT_WINDOW_SECONDS,
    )
    auth_failure_limiter = InMemoryRateLimiter(
        limit=API_AUTH_FAILURE_LIMIT,
        window_seconds=API_AUTH_FAILURE_WINDOW_SECONDS,
    )
    operator_contract = settings.api_enabled

    def client_key(request: Request) -> str:
        return client_key_from_request(request, settings.api_trusted_proxy_ips)

    async def require_api_token(
        request: Request,
        x_api_token: str | None = Header(default=None, alias="X-API-Token"),
    ) -> None:
        configured_tokens: tuple[tuple[str, str], ...]
        is_web_path = request.url.path.startswith("/api/v1/web")
        is_operator_path = request.url.path.startswith("/api/v1/tickets")
        if is_web_path:
            if not settings.web_api_enabled:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            configured_tokens = (
                (("web", settings.web_api_token.get_secret_value()),)
                if settings.web_api_token is not None
                else ()
            )
            unsafe_auth = False
            realm = "web"
        elif is_operator_path:
            if not operator_contract:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
            configured_tokens = (
                (("operator", settings.api_admin_token.get_secret_value()),)
                if settings.api_admin_token is not None
                else ()
            )
            unsafe_auth = settings.api_unsafe_disable_auth
            realm = "operator"
        else:
            configured_tokens = tuple(
                (token_realm, token.get_secret_value())
                for token_realm, enabled, token in (
                    ("operator", operator_contract, settings.api_admin_token),
                    ("web", settings.web_api_enabled, settings.web_api_token),
                )
                if enabled and token is not None
            )
            unsafe_auth = settings.api_unsafe_disable_auth
            realm = "common"

        if unsafe_auth:
            allowed, retry_after = await request_limiter.consume("request:operator-unsafe")
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests",
                    headers={"Retry-After": str(retry_after)},
                )
            return
        auth_key = f"auth:{realm}:{client_key(request)}"
        matched_realms = [
            token_realm
            for token_realm, configured_token in configured_tokens
            if x_api_token is not None and secrets.compare_digest(x_api_token, configured_token)
        ]
        if not matched_realms:
            allowed, retry_after = await auth_failure_limiter.consume(auth_key)
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many authentication attempts",
                    headers={"Retry-After": str(retry_after)},
                )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        await auth_failure_limiter.reset(auth_key)
        allowed, retry_after = await request_limiter.consume(f"request:{matched_realms[0]}")
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests",
                headers={"Retry-After": str(retry_after)},
            )

    app = FastAPI(
        title="Resolvate API",
        version=PROJECT_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(require_api_token)],
    )
    app.router.add_event_handler("startup", lambda: runtime_health.ready("api"))

    def error_response(
        *,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "trace_id": trace_id_var.get(),
                }
            },
            headers=headers,
        )

    @app.middleware("http")
    async def trace_audit(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = uuid.uuid4().hex
        trace_token = trace_id_var.set(trace_id)
        started_at = time.monotonic()
        try:
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
            component = (
                "web_api"
                if request.url.path.startswith("/api/v1/web")
                else "operator_api"
                if request.url.path.startswith("/api/v1/tickets")
                else "api"
            )
            metrics.observe_request(
                component,
                f"http_{response.status_code // 100}xx",
                time.monotonic() - started_at,
            )
            if response.status_code >= 400 or not getattr(
                request.state, "suppress_success_completion_log", False
            ):
                record_event(
                    "api_request_completed",
                    http_method=request.method,
                    http_path=request.url.path,
                    http_status=response.status_code,
                    duration_ms=round((time.monotonic() - started_at) * 1000, 2),
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
        status.HTTP_413_CONTENT_TOO_LARGE: ("payload_too_large", "Payload too large"),
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: (
            "unsupported_media_type",
            "Unsupported media type",
        ),
        status.HTTP_422_UNPROCESSABLE_CONTENT: (
            "validation_error",
            "Request validation failed",
        ),
        status.HTTP_429_TOO_MANY_REQUESTS: ("rate_limited", "Too many requests"),
        status.HTTP_503_SERVICE_UNAVAILABLE: ("service_unavailable", "Service unavailable"),
    }

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, error: HTTPException) -> JSONResponse:
        del request
        code, message = safe_http_errors.get(
            error.status_code, ("request_failed", "Request failed")
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

    @app.exception_handler(TicketNotFoundError)
    async def handle_ticket_not_found(request: Request, error: TicketNotFoundError) -> JSONResponse:
        del request, error
        return error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="Resource not found",
        )

    register_routes(
        app,
        database=database,
        ticket_service=ticket_service,
        runtime_health=runtime_health,
        metrics=metrics,
    )
    if settings.web_api_enabled:
        register_web_routes(
            app,
            ticket_service=ticket_service,
            settings=settings,
            media_storage=media_storage,
            metrics=metrics,
            user_message_limiter=user_message_limiter,
        )

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schemes: dict[str, object] = {}
        if operator_contract and not settings.api_unsafe_disable_auth:
            schemes["OperatorApiToken"] = {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Token",
                "description": "Operator API credential",
            }
        if settings.web_api_enabled:
            schemes["WebApiToken"] = {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Token",
                "description": "Web Support API credential; never expose it to a browser",
            }
        components = schema.setdefault("components", {})
        if schemes and isinstance(components, dict):
            components["securitySchemes"] = schemes
        paths = schema.get("paths", {})
        if isinstance(paths, dict):
            for path, methods in list(paths.items()):
                if not isinstance(path, str) or not isinstance(methods, dict):
                    continue
                if path.startswith("/api/v1/tickets") and not operator_contract:
                    del paths[path]
                    continue
                if path.startswith("/api/v1/web"):
                    security: list[dict[str, list[str]]] = [{"WebApiToken": []}]
                elif path.startswith("/api/v1/tickets"):
                    security = (
                        [] if settings.api_unsafe_disable_auth else [{"OperatorApiToken": []}]
                    )
                else:
                    if settings.api_unsafe_disable_auth:
                        security = []
                    else:
                        security = []
                        if operator_contract:
                            security.append({"OperatorApiToken": []})
                        if settings.web_api_enabled:
                            security.append({"WebApiToken": []})
                for operation in methods.values():
                    if not isinstance(operation, dict):
                        continue
                    operation["security"] = security
                    parameters = operation.get("parameters")
                    if isinstance(parameters, list):
                        operation["parameters"] = [
                            parameter
                            for parameter in parameters
                            if not (
                                isinstance(parameter, dict)
                                and str(parameter.get("name", "")).casefold() == "x-api-token"
                            )
                        ]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
