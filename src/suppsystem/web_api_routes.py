from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException
from starlette.types import Message

from suppsystem.api_idempotency import api_idempotency_command
from suppsystem.api_schemas import IdempotencyKey, TicketId
from suppsystem.config import Settings
from suppsystem.media_storage import (
    MAX_WEB_PHOTO_BYTES,
    LocalMediaStorage,
    MediaValidationError,
    StoredMedia,
)
from suppsystem.metrics import MetricsRegistry
from suppsystem.service_types import TicketNotFoundError
from suppsystem.services import TicketService
from suppsystem.user_message_limits import UserMessageRateLimiter
from suppsystem.web_api_schemas import (
    WebAcceptedMessageResponse,
    WebBlockRequest,
    WebConversationResponse,
    WebMessageRequest,
    WebMessagesResponse,
    WebMutationResponse,
    WebRatingRequest,
    accepted_message_response,
    conversation_response,
    message_response,
)

MAX_WEB_MESSAGE_REQUEST_BYTES = MAX_WEB_PHOTO_BYTES + 64 * 1024
MAX_TELEGRAM_PHOTO_CAPTION_CHARS = 1024
logger = logging.getLogger(__name__)


async def _delete_media_if_unlinked(
    *,
    ticket_service: TicketService,
    media_storage: LocalMediaStorage,
    stored_media: StoredMedia,
) -> None:
    try:
        await ticket_service.get_media(stored_media.id)
    except TicketNotFoundError:
        await media_storage.delete(stored_media)
        return
    except Exception:
        logger.warning(
            "Unable to determine whether Web media is linked; retaining it for orphan cleanup",
            exc_info=True,
            extra={"event": "web_media_link_check_failed", "media_id": stored_media.id},
        )


def _limit_request_body(request: Request, max_bytes: int) -> None:
    original_receive = request.receive
    received = 0

    async def limited_receive() -> Message:
        nonlocal received
        message = await original_receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Web message request is too large",
                )
        return message

    request._receive = limited_receive


async def _parse_message_request(
    request: Request,
) -> tuple[WebMessageRequest, StarletteUploadFile | None]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise ValueError
            if parsed_content_length > MAX_WEB_MESSAGE_REQUEST_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Web message request is too large",
                )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length",
            ) from error
    _limit_request_body(request, MAX_WEB_MESSAGE_REQUEST_BYTES)
    content_type = request.headers.get("content-type", "").casefold()
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
            return WebMessageRequest.model_validate(payload), None
        except (ValueError, ValidationError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid Web message payload",
            ) from error
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form(max_files=1, max_fields=5, max_part_size=8 * 1024)
            allowed_fields = {
                "external_user_id",
                "email",
                "display_name",
                "remnawave_user_uuid",
                "text",
                "photo",
            }
            form_items = form.multi_items()
            field_names = [key for key, _value in form_items]
            if any(key not in allowed_fields for key in field_names) or len(field_names) != len(
                set(field_names)
            ):
                raise ValueError("unknown or repeated multipart field")
            photo_value = form.get("photo")
            photo = photo_value if isinstance(photo_value, StarletteUploadFile) else None
            if photo_value is not None and photo is None:
                raise ValueError("photo must be a file")
            payload = {
                key: value
                for key in (
                    "external_user_id",
                    "email",
                    "display_name",
                    "remnawave_user_uuid",
                    "text",
                )
                if isinstance((value := form.get(key)), str)
            }
            return WebMessageRequest.model_validate(payload), photo
        except (ValueError, ValidationError, MultiPartException) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid Web message payload",
            ) from error
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail="Content-Type must be application/json or multipart/form-data",
    )


def register_web_routes(
    app: FastAPI,
    *,
    ticket_service: TicketService,
    settings: Settings,
    media_storage: LocalMediaStorage,
    metrics: MetricsRegistry,
    user_message_limiter: UserMessageRateLimiter,
) -> None:
    @app.post(
        "/api/v1/web/messages",
        response_model=WebAcceptedMessageResponse,
        tags=["web-support"],
    )
    async def create_web_message(
        request: Request,
        x_idempotency_key: IdempotencyKey,
    ) -> WebAcceptedMessageResponse:
        message, upload = await _parse_message_request(request)
        if message.text is None and upload is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="text or photo is required",
            )
        if (
            upload is not None
            and message.text is not None
            and len(message.text) > MAX_TELEGRAM_PHOTO_CAPTION_CHARS
        ):
            await upload.close()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="photo caption must not exceed 1024 characters",
            )
        identity_resource = (
            (message.external_user_id or "").strip()
            if settings.web_identity_mode == "external_id"
            else message.email.strip().casefold()
        )
        if not identity_resource:
            if upload is not None:
                await upload.close()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="external_user_id is required",
            )
        stored_media: StoredMedia | None = None
        try:
            if upload is not None:
                stored_media = await media_storage.save_upload(upload)
                metrics.event("web_media", "uploaded")
            command = api_idempotency_command(
                operation="web_message",
                resource=identity_resource or "missing",
                key=x_idempotency_key,
                payload={
                    "external_user_id": message.external_user_id,
                    "email": message.email.strip().casefold(),
                    "display_name": message.display_name,
                    "remnawave_user_uuid": message.remnawave_user_uuid,
                    "text": message.text,
                    "photo_sha256": stored_media.sha256 if stored_media else None,
                },
            )
            replay = await ticket_service.load_web_message_replay(command)
            if replay is not None:
                if stored_media is not None:
                    await media_storage.delete(stored_media)
                    stored_media = None
                metrics.event("web_ingress", "replayed")
                return accepted_message_response(replay)
            rate_limit = await user_message_limiter.consume(
                f"web:{settings.web_identity_mode}:{identity_resource}"
            )
            if not rate_limit.allowed:
                if stored_media is not None:
                    await media_storage.delete(stored_media)
                    stored_media = None
                metrics.event("web_ingress", "rate_limited")
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many user messages",
                    headers={"Retry-After": str(rate_limit.retry_after_seconds)},
                )
            try:
                result = await ticket_service.accept_message(
                    identity_mode=settings.web_identity_mode,
                    external_user_id=message.external_user_id,
                    email=message.email,
                    display_name=message.display_name,
                    remnawave_user_uuid=message.remnawave_user_uuid,
                    content=message.text,
                    media=stored_media,
                    target_chat_id=settings.support_group_id,
                    command=command,
                )
            except IntegrityError:
                # A concurrent request may commit the same idempotency key after
                # our initial replay lookup. Resolve that race to its durable result.
                replay = await ticket_service.load_web_message_replay(command)
                if replay is None:
                    raise
                result = replay
        except (ValueError, MediaValidationError) as error:
            if stored_media is not None:
                await media_storage.delete(stored_media)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        except Exception:
            if stored_media is not None:
                await _delete_media_if_unlinked(
                    ticket_service=ticket_service,
                    media_storage=media_storage,
                    stored_media=stored_media,
                )
            raise
        if stored_media is not None:
            await _delete_media_if_unlinked(
                ticket_service=ticket_service,
                media_storage=media_storage,
                stored_media=stored_media,
            )
        metrics.event("web_ingress", "replayed" if not result.changed else "accepted")
        return accepted_message_response(result)

    @app.get(
        "/api/v1/web/conversations/{ticket_id}",
        response_model=WebConversationResponse,
        tags=["web-support"],
    )
    async def get_web_conversation(
        ticket_id: TicketId, request: Request
    ) -> WebConversationResponse:
        request.state.suppress_success_completion_log = True
        return conversation_response(await ticket_service.get_web_ticket(ticket_id))

    @app.get(
        "/api/v1/web/conversations/{ticket_id}/messages",
        response_model=WebMessagesResponse,
        tags=["web-support"],
    )
    async def get_web_messages(
        ticket_id: TicketId,
        request: Request,
        after: str | None = Query(default=None, min_length=8, max_length=512),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> WebMessagesResponse:
        request.state.suppress_success_completion_log = True
        try:
            page = await ticket_service.list_messages(ticket_id, after=after, limit=limit)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid message cursor",
            ) from error
        metrics.event("web_polling", "empty" if not page.items else "messages")
        return WebMessagesResponse(
            items=[message_response(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @app.post(
        "/api/v1/web/conversations/{ticket_id}/close",
        response_model=WebMutationResponse,
        tags=["web-support"],
    )
    async def close_web_conversation(
        ticket_id: TicketId,
        x_idempotency_key: IdempotencyKey,
    ) -> WebMutationResponse:
        await ticket_service.get_web_ticket(ticket_id)
        command = api_idempotency_command(
            operation="web_close", resource=ticket_id, key=x_idempotency_key, payload={}
        )
        changed = await ticket_service.close(
            ticket_id=ticket_id,
            operator_telegram_id=0,
            idempotency_key=command.storage_key,
            api_idempotency=command,
        )
        return WebMutationResponse(changed=changed)

    @app.post(
        "/api/v1/web/conversations/{ticket_id}/block",
        response_model=WebMutationResponse,
        tags=["web-support"],
    )
    async def block_web_conversation(
        ticket_id: TicketId,
        request: WebBlockRequest,
        x_idempotency_key: IdempotencyKey,
    ) -> WebMutationResponse:
        await ticket_service.get_web_ticket(ticket_id)
        command = api_idempotency_command(
            operation="web_block",
            resource=ticket_id,
            key=x_idempotency_key,
            payload={"reason": request.reason},
        )
        changed = await ticket_service.block_ticket(
            ticket_id=ticket_id,
            operator_telegram_id=0,
            reason=request.reason,
            source="web_api",
            idempotency_key=command.storage_key,
            api_idempotency=command,
        )
        metrics.event("web_support_block", "blocked" if changed else "unchanged")
        return WebMutationResponse(changed=changed)

    @app.post(
        "/api/v1/web/conversations/{ticket_id}/unblock",
        response_model=WebMutationResponse,
        tags=["web-support"],
    )
    async def unblock_web_conversation(
        ticket_id: TicketId,
        x_idempotency_key: IdempotencyKey,
    ) -> WebMutationResponse:
        await ticket_service.get_web_ticket(ticket_id)
        command = api_idempotency_command(
            operation="web_unblock", resource=ticket_id, key=x_idempotency_key, payload={}
        )
        changed = await ticket_service.unblock_ticket(
            ticket_id=ticket_id,
            operator_telegram_id=0,
            source="web_api",
            idempotency_key=command.storage_key,
            api_idempotency=command,
        )
        metrics.event("web_support_block", "unblocked" if changed else "unchanged")
        return WebMutationResponse(changed=changed)

    @app.post(
        "/api/v1/web/conversations/{ticket_id}/rating",
        response_model=WebMutationResponse,
        tags=["web-support"],
    )
    async def rate_web_conversation(
        ticket_id: TicketId,
        request: WebRatingRequest,
        x_idempotency_key: IdempotencyKey,
    ) -> WebMutationResponse:
        command = api_idempotency_command(
            operation="web_rating",
            resource=ticket_id,
            key=x_idempotency_key,
            payload={"score": request.score},
        )
        changed = await ticket_service.submit_rating(
            ticket_id=ticket_id,
            score=request.score,
            target_chat_id=settings.support_group_id,
            command=command,
        )
        return WebMutationResponse(changed=changed)

    @app.get("/api/v1/web/media/{media_id}", tags=["web-support"])
    async def get_web_media(media_id: TicketId) -> FileResponse:
        media = await ticket_service.get_media(media_id)
        path = await media_storage.resolve_file(media.storage_path)
        if path is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
        metrics.event("web_media", "downloaded")
        return FileResponse(
            path,
            media_type=media.mime_type,
            filename=None,
            headers={"Cache-Control": "private, max-age=3600"},
        )
