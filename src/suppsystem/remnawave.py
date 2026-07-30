from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from suppsystem.metrics import MetricsRegistry


class RemnawaveError(Exception):
    pass


class RemnawaveAuthError(RemnawaveError):
    pass


class RemnawaveNotFoundError(RemnawaveError):
    pass


class RemnawaveAmbiguousIdentityError(RemnawaveError):
    pass


class RemnawaveRateLimitedError(RemnawaveError):
    pass


class RemnawaveValidationError(RemnawaveError):
    pass


class RemnawaveUnavailableError(RemnawaveError):
    pass


class RemnawaveUnknownOutcomeError(RemnawaveError):
    pass


class RemnawaveUnexpectedResponseError(RemnawaveError):
    pass


@dataclass(frozen=True)
class RemnawaveTraffic:
    used_traffic_bytes: int | None
    lifetime_used_traffic_bytes: int | None
    online_at: datetime | None


@dataclass(frozen=True)
class RemnawaveUser:
    uuid: str
    id: int
    short_uuid: str
    username: str
    status: str
    expire_at: datetime
    subscription_url: str
    telegram_id: int | None
    email: str | None
    hwid_device_limit: int | None
    traffic: RemnawaveTraffic | None
    credential_fingerprint: str | None = None


@dataclass(frozen=True)
class RemnawaveBulkActionResult:
    affected_rows: int


@dataclass(frozen=True)
class RemnawaveHwidDevice:
    hwid: str
    user_uuid: str
    platform: str | None
    os_version: str | None
    device_model: str | None
    user_agent: str | None


@dataclass(frozen=True)
class RemnawaveHwidDeviceResetResult:
    total: int
    devices: list[RemnawaveHwidDevice]


class RemnawaveClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: SecretStr,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds
        self._client = client
        self.metrics = metrics

    async def get_user_by_telegram_id(self, telegram_id: int) -> RemnawaveUser:
        users = await self._request_user_array(f"/api/users/by-telegram-id/{telegram_id}")
        return self._single_user(users, lookup="telegram ID")

    async def get_user_by_email(self, email: str) -> RemnawaveUser:
        users = await self._request_user_array("/api/users/by-email/" + quote(email, safe=""))
        return self._single_user(users, lookup="email")

    async def get_user_by_username(self, username: str) -> RemnawaveUser:
        payload = await self._request_json("/api/users/by-username/" + quote(username, safe=""))
        response = payload.get("response")
        if not isinstance(response, dict):
            raise RemnawaveUnexpectedResponseError("Remnawave user response is malformed")
        return self._parse_user(response)

    async def extend_user_expiration(
        self, *, user_uuid: str, extend_days: int
    ) -> RemnawaveBulkActionResult:
        payload = await self._request_json(
            "/api/users/bulk/extend-expiration-date",
            method="POST",
            json={"uuids": [user_uuid], "extendDays": extend_days},
            mutation=True,
        )
        try:
            response = payload.get("response")
            if not isinstance(response, dict):
                raise RemnawaveUnexpectedResponseError("Remnawave extend response is malformed")
            affected_rows = _required_int(response, "affectedRows")
            if affected_rows != 1:
                raise RemnawaveUnexpectedResponseError(
                    "Remnawave extend response has an unexpected affected row count"
                )
            return RemnawaveBulkActionResult(affected_rows=affected_rows)
        except RemnawaveUnexpectedResponseError as error:
            raise RemnawaveUnknownOutcomeError(
                "Remnawave extend outcome cannot be confirmed"
            ) from error

    async def revoke_user_subscription(
        self, *, user_uuid: str, revoke_only_passwords: bool
    ) -> RemnawaveUser:
        payload = await self._request_json(
            f"/api/users/{quote(user_uuid, safe='')}/actions/revoke",
            method="POST",
            json={"revokeOnlyPasswords": revoke_only_passwords},
            mutation=True,
        )
        try:
            response = payload.get("response")
            if not isinstance(response, dict):
                raise RemnawaveUnexpectedResponseError("Remnawave revoke response is malformed")
            return self._parse_user(response)
        except RemnawaveUnexpectedResponseError as error:
            raise RemnawaveUnknownOutcomeError(
                "Remnawave revoke outcome cannot be confirmed"
            ) from error

    async def reset_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        payload = await self._request_json(
            "/api/hwid/devices/delete-all",
            method="POST",
            json={"userUuid": user_uuid},
            mutation=True,
        )
        try:
            return self._parse_hwid_devices(payload)
        except RemnawaveUnexpectedResponseError as error:
            raise RemnawaveUnknownOutcomeError(
                "Remnawave HWID reset outcome cannot be confirmed"
            ) from error

    async def get_user_hwid_devices(self, *, user_uuid: str) -> RemnawaveHwidDeviceResetResult:
        payload = await self._request_json("/api/hwid/devices/" + quote(user_uuid, safe=""))
        return self._parse_hwid_devices(payload)

    async def _request_user_array(self, path: str) -> list[RemnawaveUser]:
        payload = await self._request_json(path)
        response = payload.get("response")
        if not isinstance(response, list):
            raise RemnawaveUnexpectedResponseError("Remnawave users response is malformed")
        return [self._parse_user(item) for item in response]

    async def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        json: dict[str, Any] | None = None,
        mutation: bool = False,
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient()
        should_close = self._client is None
        started_at = time.monotonic()
        outcome = "request_error"
        try:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.api_token.get_secret_value()}"},
                json=json,
                timeout=self.timeout_seconds,
            )
            outcome = f"http_{response.status_code // 100}xx"
        except httpx.TimeoutException as error:
            if mutation:
                raise RemnawaveUnknownOutcomeError("Remnawave mutation timed out") from error
            raise RemnawaveUnavailableError("Remnawave request timed out") from error
        except httpx.HTTPError as error:
            if mutation:
                raise RemnawaveUnknownOutcomeError(
                    "Remnawave mutation outcome is unknown"
                ) from error
            raise RemnawaveUnavailableError("Remnawave request failed") from error
        finally:
            if self.metrics is not None:
                self.metrics.observe_request("remnawave", outcome, time.monotonic() - started_at)
            if should_close:
                await client.aclose()

        if response.status_code in {401, 403}:
            raise RemnawaveAuthError("Remnawave authentication failed")
        if response.status_code == 404:
            raise RemnawaveNotFoundError("Remnawave user not found")
        if response.status_code == 429:
            raise RemnawaveRateLimitedError("Remnawave rate limit exceeded")
        if response.status_code >= 500:
            if mutation:
                raise RemnawaveUnknownOutcomeError("Remnawave mutation returned a server error")
            raise RemnawaveUnavailableError("Remnawave is unavailable")
        if response.status_code == 400:
            raise RemnawaveValidationError("Remnawave validation failed")
        if response.status_code >= 400:
            raise RemnawaveUnexpectedResponseError(
                f"Unexpected Remnawave status code: {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as error:
            if mutation:
                raise RemnawaveUnknownOutcomeError(
                    "Remnawave mutation returned invalid JSON"
                ) from error
            raise RemnawaveUnexpectedResponseError("Remnawave returned invalid JSON") from error
        if not isinstance(payload, dict):
            if mutation:
                raise RemnawaveUnknownOutcomeError("Remnawave mutation returned non-object JSON")
            raise RemnawaveUnexpectedResponseError("Remnawave returned non-object JSON")
        return payload

    @staticmethod
    def _single_user(users: list[RemnawaveUser], *, lookup: str) -> RemnawaveUser:
        if len(users) == 1:
            return users[0]
        if not users:
            raise RemnawaveNotFoundError(f"Remnawave user not found by {lookup}")
        raise RemnawaveAmbiguousIdentityError(f"Remnawave returned multiple users for one {lookup}")

    @classmethod
    def _parse_user(cls, payload: object) -> RemnawaveUser:
        if not isinstance(payload, dict):
            raise RemnawaveUnexpectedResponseError("Remnawave user item is malformed")
        try:
            traffic_payload = payload.get("userTraffic")
            traffic = (
                RemnawaveTraffic(
                    used_traffic_bytes=_optional_int(traffic_payload.get("usedTrafficBytes")),
                    lifetime_used_traffic_bytes=_optional_int(
                        traffic_payload.get("lifetimeUsedTrafficBytes")
                    ),
                    online_at=_parse_optional_datetime(traffic_payload.get("onlineAt")),
                )
                if isinstance(traffic_payload, dict)
                else None
            )
            return RemnawaveUser(
                uuid=str(payload["uuid"]),
                id=int(payload["id"]),
                short_uuid=str(payload["shortUuid"]),
                username=str(payload["username"]),
                status=str(payload["status"]),
                expire_at=_parse_datetime(payload["expireAt"]),
                subscription_url=str(payload["subscriptionUrl"]),
                telegram_id=_optional_int(payload.get("telegramId")),
                email=_optional_str(payload.get("email")),
                hwid_device_limit=_optional_int(payload.get("hwidDeviceLimit")),
                traffic=traffic,
                credential_fingerprint=_credential_fingerprint(payload),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RemnawaveUnexpectedResponseError("Remnawave user payload is malformed") from error

    @classmethod
    def _parse_hwid_device(cls, payload: object) -> RemnawaveHwidDevice:
        if not isinstance(payload, dict):
            raise RemnawaveUnexpectedResponseError("Remnawave HWID device item is malformed")
        try:
            return RemnawaveHwidDevice(
                hwid=str(payload["hwid"]),
                user_uuid=str(payload["userUuid"]),
                platform=_optional_str(payload.get("platform")),
                os_version=_optional_str(payload.get("osVersion")),
                device_model=_optional_str(payload.get("deviceModel")),
                user_agent=_optional_str(payload.get("userAgent")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RemnawaveUnexpectedResponseError(
                "Remnawave HWID device payload is malformed"
            ) from error

    @classmethod
    def _parse_hwid_devices(cls, payload: dict[str, Any]) -> RemnawaveHwidDeviceResetResult:
        response = payload.get("response")
        if not isinstance(response, dict):
            raise RemnawaveUnexpectedResponseError("Remnawave HWID response is malformed")
        devices_payload = response.get("devices")
        if not isinstance(devices_payload, list):
            raise RemnawaveUnexpectedResponseError("Remnawave HWID devices response is malformed")
        return RemnawaveHwidDeviceResetResult(
            total=_required_int(response, "total"),
            devices=[cls._parse_hwid_device(item) for item in devices_payload],
        )


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("datetime value must be a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("boolean is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str | bytes | bytearray):
        return int(value)
    raise TypeError("integer value must be an integer or numeric string")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _required_int(payload: dict[str, Any], key: str) -> int:
    try:
        value = payload[key]
        if isinstance(value, bool):
            raise TypeError("boolean is not an integer")
        return int(value)
    except (KeyError, TypeError, ValueError) as error:
        raise RemnawaveUnexpectedResponseError(
            f"Remnawave response field {key} is malformed"
        ) from error


def _credential_fingerprint(payload: dict[str, Any]) -> str | None:
    values = [payload.get(key) for key in ("trojanPassword", "vlessUuid", "ssPassword")]
    if any(not isinstance(value, str) for value in values):
        return None
    credentials = (value for value in values if isinstance(value, str))
    return hashlib.sha256("\0".join(credentials).encode()).hexdigest()
