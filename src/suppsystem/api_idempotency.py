from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suppsystem.models import OperatorAction

API_IDEMPOTENCY_VERSION = 1


class ApiIdempotencyConflictError(Exception):
    """The scoped key already belongs to a different canonical request."""


@dataclass(frozen=True)
class ApiIdempotencyCommand:
    operation: str
    resource: str
    key: str
    fingerprint: str

    @property
    def storage_key(self) -> str:
        return f"api:{self.operation}:{self.resource}:{self.key}"


def api_idempotency_command(
    *,
    operation: str,
    resource: str,
    key: str,
    payload: dict[str, object],
) -> ApiIdempotencyCommand:
    canonical_payload = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return ApiIdempotencyCommand(
        operation=operation,
        resource=resource,
        key=key,
        fingerprint=hashlib.sha256(canonical_payload).hexdigest(),
    )


def api_action_payload(
    base_payload: dict[str, object],
    *,
    command: ApiIdempotencyCommand | None,
    changed: bool,
    response: dict[str, object] | None = None,
) -> dict[str, object]:
    if command is None:
        return base_payload
    canonical_response = dict(response or {})
    canonical_response["changed"] = changed
    return {
        **base_payload,
        "api_idempotency": {
            "version": API_IDEMPOTENCY_VERSION,
            "operation": command.operation,
            "resource": command.resource,
            "fingerprint": command.fingerprint,
            "response": canonical_response,
        },
    }


async def load_api_replay_response(
    session: AsyncSession,
    command: ApiIdempotencyCommand | None,
) -> dict[str, object] | None:
    if command is None:
        return None
    action = await session.scalar(
        select(OperatorAction).where(OperatorAction.idempotency_key == command.storage_key)
    )
    if action is None:
        return None
    metadata = action.payload.get("api_idempotency")
    if not isinstance(metadata, dict):
        raise ApiIdempotencyConflictError(command.storage_key)
    if (
        metadata.get("version") != API_IDEMPOTENCY_VERSION
        or metadata.get("operation") != command.operation
        or metadata.get("resource") != command.resource
        or metadata.get("fingerprint") != command.fingerprint
    ):
        raise ApiIdempotencyConflictError(command.storage_key)
    response = metadata.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("changed"), bool):
        raise ApiIdempotencyConflictError(command.storage_key)
    return dict(response)


async def load_api_replay(
    session: AsyncSession,
    command: ApiIdempotencyCommand | None,
) -> bool | None:
    response = await load_api_replay_response(session, command)
    return None if response is None else bool(response["changed"])
