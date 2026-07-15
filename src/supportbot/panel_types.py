from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from supportbot.remnawave import (
    RemnawaveAmbiguousIdentityError,
    RemnawaveAuthError,
    RemnawaveBulkActionResult,
    RemnawaveError,
    RemnawaveHwidDeviceResetResult,
    RemnawaveNotFoundError,
    RemnawaveRateLimitedError,
    RemnawaveUnavailableError,
    RemnawaveUnexpectedResponseError,
    RemnawaveUnknownOutcomeError,
    RemnawaveUser,
    RemnawaveValidationError,
)

PanelLookupStatus = Literal[
    "found",
    "not_found",
    "ambiguous_identity",
    "auth_error",
    "rate_limited",
    "validation_error",
    "unavailable",
    "unexpected_response",
]
PanelActionStatus = Literal[
    "completed",
    "duplicate",
    "not_found",
    "ambiguous_identity",
    "auth_error",
    "rate_limited",
    "validation_error",
    "unavailable",
    "not_applied",
    "unknown",
    "needs_reconcile",
    "unexpected_response",
]


class RemnawaveReader(Protocol):
    async def get_user_by_telegram_id(self, telegram_id: int) -> RemnawaveUser: ...
    async def get_user_by_email(self, email: str) -> RemnawaveUser: ...


class RemnawaveOperator(RemnawaveReader, Protocol):
    async def extend_user_expiration(
        self, *, user_uuid: str, extend_days: int
    ) -> RemnawaveBulkActionResult: ...
    async def revoke_user_subscription(
        self, *, user_uuid: str, revoke_only_passwords: bool
    ) -> RemnawaveUser: ...
    async def reset_user_hwid_devices(
        self, *, user_uuid: str
    ) -> RemnawaveHwidDeviceResetResult: ...


@dataclass(frozen=True)
class PanelSubscriptionInfo:
    uuid: str
    username: str
    status: str
    expire_at: datetime
    subscription_url: str
    telegram_id: int | None
    email: str | None
    hwid_device_limit: int | None
    used_traffic_bytes: int | None
    lifetime_used_traffic_bytes: int | None
    online_at: datetime | None


@dataclass(frozen=True)
class PanelSubscriptionLookup:
    status: PanelLookupStatus
    identity_provider: str
    identity_value: str
    subscription: PanelSubscriptionInfo | None = None

    @property
    def found(self) -> bool:
        return self.status == "found" and self.subscription is not None


@dataclass(frozen=True)
class PanelActionResult:
    action: str
    status: PanelActionStatus
    changed: bool
    identity_provider: str
    identity_value: str
    subscription: PanelSubscriptionInfo | None = None
    affected_rows: int | None = None
    devices_removed: int | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed" and self.changed


Mutation = Callable[[RemnawaveUser], Awaitable[tuple[PanelActionStatus, dict[str, Any]]]]


def lookup_status_from_error(error: RemnawaveError) -> PanelLookupStatus:
    if isinstance(error, RemnawaveNotFoundError):
        return "not_found"
    if isinstance(error, RemnawaveAmbiguousIdentityError):
        return "ambiguous_identity"
    if isinstance(error, RemnawaveAuthError):
        return "auth_error"
    if isinstance(error, RemnawaveRateLimitedError):
        return "rate_limited"
    if isinstance(error, RemnawaveValidationError):
        return "validation_error"
    if isinstance(error, RemnawaveUnavailableError):
        return "unavailable"
    if isinstance(error, RemnawaveUnexpectedResponseError):
        return "unexpected_response"
    return "unexpected_response"


def action_status_from_lookup(status: PanelLookupStatus) -> PanelActionStatus:
    return "unexpected_response" if status == "found" else status


def action_status_from_error(error: RemnawaveError) -> PanelActionStatus:
    if isinstance(error, RemnawaveUnknownOutcomeError):
        return "unknown"
    return action_status_from_lookup(lookup_status_from_error(error))


def mutation_user(subscription: PanelSubscriptionInfo) -> RemnawaveUser:
    return RemnawaveUser(
        uuid=subscription.uuid,
        id=0,
        short_uuid="",
        username=subscription.username,
        status=subscription.status,
        expire_at=subscription.expire_at,
        subscription_url=subscription.subscription_url,
        telegram_id=subscription.telegram_id,
        email=subscription.email,
        hwid_device_limit=subscription.hwid_device_limit,
        traffic=None,
    )


def mutation_visible(
    *,
    action: str,
    request_payload: dict[str, Any],
    before: PanelSubscriptionInfo,
    after: PanelSubscriptionInfo,
) -> bool:
    if action == "extend_subscription":
        extend_days = request_payload.get("extend_days")
        return isinstance(extend_days, int) and after.expire_at >= (
            before.expire_at + timedelta(days=extend_days)
        )
    if action == "revoke_subscription_link":
        return after.subscription_url != before.subscription_url
    return False


def safe_subscription_context(subscription: PanelSubscriptionInfo) -> dict[str, Any]:
    return {
        "remnawave_uuid": subscription.uuid,
        "remnawave_username": subscription.username,
        "remnawave_status": subscription.status,
        "remnawave_telegram_id": subscription.telegram_id,
        "remnawave_email": subscription.email,
    }


def safe_user_context(user: RemnawaveUser) -> dict[str, Any]:
    return {
        "remnawave_uuid": user.uuid,
        "remnawave_username": user.username,
        "remnawave_status": user.status,
        "remnawave_telegram_id": user.telegram_id,
        "remnawave_email": user.email,
    }


def optional_result_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def subscription_info(user: RemnawaveUser) -> PanelSubscriptionInfo:
    return PanelSubscriptionInfo(
        uuid=user.uuid,
        username=user.username,
        status=user.status,
        expire_at=user.expire_at,
        subscription_url=user.subscription_url,
        telegram_id=user.telegram_id,
        email=user.email,
        hwid_device_limit=user.hwid_device_limit,
        used_traffic_bytes=user.traffic.used_traffic_bytes if user.traffic else None,
        lifetime_used_traffic_bytes=(
            user.traffic.lifetime_used_traffic_bytes if user.traffic else None
        ),
        online_at=user.traffic.online_at if user.traffic else None,
    )
