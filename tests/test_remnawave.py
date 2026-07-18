from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from supportbot.remnawave import (
    RemnawaveAmbiguousIdentityError,
    RemnawaveAuthError,
    RemnawaveClient,
    RemnawaveNotFoundError,
    RemnawaveRateLimitedError,
    RemnawaveUnavailableError,
    RemnawaveUnknownOutcomeError,
    RemnawaveValidationError,
)


def user_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "uuid": "11111111-1111-1111-1111-111111111111",
        "id": 123,
        "shortUuid": "abc123",
        "username": "user123",
        "status": "ACTIVE",
        "expireAt": "2026-07-25T12:00:00.000Z",
        "subscriptionUrl": "https://sub.example/abc123",
        "telegramId": 123456789,
        "email": "user@example.com",
        "hwidDeviceLimit": 5,
        "trojanPassword": "trojan-secret",
        "vlessUuid": "22222222-2222-2222-2222-222222222222",
        "ssPassword": "shadowsocks-secret",
        "userTraffic": {
            "usedTrafficBytes": 100,
            "lifetimeUsedTrafficBytes": 200,
            "onlineAt": None,
        },
    }
    payload.update(overrides)
    return payload


def remnawave_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> RemnawaveClient:
    return RemnawaveClient(
        base_url="https://remna.example",
        api_token=SecretStr("secret-token"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_get_user_by_telegram_id_sends_bearer_and_parses_user() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"response": [user_payload()]})

    client = remnawave_client(handler)

    user = await client.get_user_by_telegram_id(123456789)

    assert seen == {
        "url": "https://remna.example/api/users/by-telegram-id/123456789",
        "auth": "Bearer secret-token",
    }
    assert user.uuid == "11111111-1111-1111-1111-111111111111"
    assert user.username == "user123"
    assert user.telegram_id == 123456789
    assert user.email == "user@example.com"
    assert user.hwid_device_limit == 5
    assert user.credential_fingerprint is not None
    assert user.traffic is not None
    assert user.traffic.used_traffic_bytes == 100


@pytest.mark.asyncio
async def test_get_user_by_email_url_encodes_email() -> None:
    seen_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        return httpx.Response(200, json={"response": [user_payload(email="user+site@example.com")]})

    client = remnawave_client(handler)

    user = await client.get_user_by_email("user+site@example.com")

    assert seen_url == "https://remna.example/api/users/by-email/user%2Bsite%40example.com"
    assert user.email == "user+site@example.com"


@pytest.mark.asyncio
async def test_get_user_by_username_parses_single_object_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://remna.example/api/users/by-username/user123"
        return httpx.Response(200, json={"response": user_payload()})

    client = remnawave_client(handler)

    user = await client.get_user_by_username("user123")

    assert user.username == "user123"


@pytest.mark.asyncio
async def test_extend_user_expiration_sends_bulk_request() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": {"affectedRows": 1}})

    client = remnawave_client(handler)

    result = await client.extend_user_expiration(
        user_uuid="11111111-1111-1111-1111-111111111111", extend_days=30
    )

    assert result.affected_rows == 1
    assert seen == {
        "method": "POST",
        "url": "https://remna.example/api/users/bulk/extend-expiration-date",
        "body": {"uuids": ["11111111-1111-1111-1111-111111111111"], "extendDays": 30},
    }


@pytest.mark.asyncio
async def test_revoke_user_subscription_sends_revoke_only_passwords() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": user_payload(shortUuid="changed")})

    client = remnawave_client(handler)

    user = await client.revoke_user_subscription(
        user_uuid="11111111-1111-1111-1111-111111111111", revoke_only_passwords=True
    )

    assert user.short_uuid == "changed"
    assert seen == {
        "method": "POST",
        "url": (
            "https://remna.example/api/users/11111111-1111-1111-1111-111111111111/actions/revoke"
        ),
        "body": {"revokeOnlyPasswords": True},
    }


@pytest.mark.asyncio
async def test_reset_user_hwid_devices_parses_deleted_devices() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "response": {
                    "total": 1,
                    "devices": [
                        {
                            "hwid": "device-hash",
                            "userUuid": "11111111-1111-1111-1111-111111111111",
                            "platform": "ios",
                            "osVersion": "17",
                            "deviceModel": "phone",
                            "userAgent": None,
                            "createdAt": "2026-06-01T00:00:00.000Z",
                            "updatedAt": "2026-06-02T00:00:00.000Z",
                        }
                    ],
                }
            },
        )

    client = remnawave_client(handler)

    result = await client.reset_user_hwid_devices(user_uuid="11111111-1111-1111-1111-111111111111")

    assert result.total == 1
    assert result.devices[0].hwid == "device-hash"
    assert result.devices[0].platform == "ios"
    assert seen == {
        "method": "POST",
        "url": "https://remna.example/api/hwid/devices/delete-all",
        "body": {"userUuid": "11111111-1111-1111-1111-111111111111"},
    }


@pytest.mark.asyncio
async def test_empty_array_is_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": []})

    client = remnawave_client(handler)

    with pytest.raises(RemnawaveNotFoundError):
        await client.get_user_by_email("missing@example.com")


@pytest.mark.asyncio
async def test_multiple_users_is_ambiguous_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": [user_payload(), user_payload(id=124)]})

    client = remnawave_client(handler)

    with pytest.raises(RemnawaveAmbiguousIdentityError):
        await client.get_user_by_telegram_id(123456789)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, RemnawaveAuthError),
        (403, RemnawaveAuthError),
        (404, RemnawaveNotFoundError),
        (429, RemnawaveRateLimitedError),
        (500, RemnawaveUnavailableError),
        (400, RemnawaveValidationError),
    ],
)
async def test_status_code_errors(
    status_code: int,
    expected_error: type[Exception],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"message": "error"})

    client = remnawave_client(handler)

    with pytest.raises(expected_error):
        await client.get_user_by_telegram_id(123456789)


@pytest.mark.asyncio
async def test_http_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = remnawave_client(handler)

    with pytest.raises(RemnawaveUnavailableError):
        await client.get_user_by_telegram_id(123456789)


@pytest.mark.asyncio
async def test_mutating_transport_error_is_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped", request=request)

    client = remnawave_client(handler)

    with pytest.raises(RemnawaveUnknownOutcomeError):
        await client.extend_user_expiration(
            user_uuid="11111111-1111-1111-1111-111111111111", extend_days=30
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": "temporary"}),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"response": {"affectedRows": "invalid"}}),
        httpx.Response(200, json={"response": {"affectedRows": 0}}),
    ],
)
@pytest.mark.asyncio
async def test_mutating_ambiguous_responses_are_unknown_outcomes(
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    client = remnawave_client(handler)

    with pytest.raises(RemnawaveUnknownOutcomeError):
        await client.extend_user_expiration(
            user_uuid="11111111-1111-1111-1111-111111111111",
            extend_days=30,
        )


@pytest.mark.asyncio
async def test_malformed_revoke_success_is_an_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": {"uuid": "incomplete"}})

    client = remnawave_client(handler)

    with pytest.raises(RemnawaveUnknownOutcomeError):
        await client.revoke_user_subscription(
            user_uuid="11111111-1111-1111-1111-111111111111",
            revoke_only_passwords=True,
        )


@pytest.mark.asyncio
async def test_get_user_hwid_devices_uses_remnawave_2_8_contract() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"response": {"total": 0, "devices": []}})

    result = await remnawave_client(handler).get_user_hwid_devices(
        user_uuid="11111111-1111-1111-1111-111111111111"
    )

    assert result.total == 0
    assert seen == {
        "method": "GET",
        "url": "https://remna.example/api/hwid/devices/11111111-1111-1111-1111-111111111111",
    }
