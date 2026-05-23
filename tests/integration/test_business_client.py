from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, cast

import httpx
import pytest
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.api.business.urls import BUSINESS_PROD_URL, BUSINESS_SANDBOX_URL
from tiktok_mcp.auth.keychain import (
    account_key,
    deserialize_account_record,
    serialize_account_record,
)
from tiktok_mcp.auth.redactor import SecretRedactor
from tiktok_mcp.observability.rate_limit_tracker import get_posture, reset_tracker
from tiktok_mcp.types.accounts import (
    Account,
    AccountStatus,
    AccountTokens,
    AccountWithTokens,
    ApiType,
)
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import AccountBrokenError, BusinessApiError

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
TEST_PATH = "/open_api/v1.3/advertiser/info/"


class CassetteInteraction(TypedDict):
    status_code: int
    headers: dict[str, str]
    body: bytes


class MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        _ = self.values.pop(key, None)

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.values if key.startswith(prefix))


@pytest.fixture(autouse=True)
def reset_business_client_state() -> Iterator[None]:
    reset_tracker()
    root_logger = logging.getLogger()
    original_filters = list(root_logger.filters)
    root_logger.filters = [
        logging_filter
        for logging_filter in root_logger.filters
        if not isinstance(logging_filter, SecretRedactor)
    ]
    yield
    reset_tracker()
    root_logger.filters = original_filters


def test_sandbox_host_routing() -> None:
    sandbox_client = BusinessAPIClient(
        _account(refresh_token="refresh-token-current", sandbox=True),
        _credentials(sandbox=True),
    )
    production_client = BusinessAPIClient(
        _account(refresh_token="refresh-token-current", sandbox=False),
        _credentials(sandbox=False),
    )

    assert sandbox_client.base_url == BUSINESS_SANDBOX_URL
    assert production_client.base_url == BUSINESS_PROD_URL


@pytest.mark.asyncio
async def test_access_token_header_and_success_tracking() -> None:
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return _business_response(request, {"advertiser_id": "adv-1"})

    async with _client(httpx.MockTransport(handler)) as client:
        result = await client.get(TEST_PATH)

    assert result == {"advertiser_id": "adv-1"}
    assert seen_headers[0]["Access-Token"] == "access-token-current"
    assert "authorization" not in seen_headers[0]
    posture = (await get_posture("business-demo"))[0]
    assert posture.api_type is ApiType.BUSINESS_ORGANIC
    assert posture.recent_request_count_last_60s == 1


@pytest.mark.asyncio
async def test_two_argument_client_loads_tokens_lazily_from_keychain() -> None:
    account_with_tokens = _account(refresh_token="refresh-token-current")
    account = _account_only(account_with_tokens)
    tokens = _tokens_only(account_with_tokens)
    backend = MemoryBackend()
    key = account_key(account.api_type, account.sandbox, account.alias)
    backend.values[key] = serialize_account_record(account, tokens)
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return _business_response(request, {"loaded": True})

    client = BusinessAPIClient(
        account,
        _credentials(),
        backend=backend,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        assert await client.get(TEST_PATH) == {"loaded": True}

    assert seen_headers[0]["Access-Token"] == "access-token-current"


@pytest.mark.asyncio
async def test_code_nonzero_raises_business_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _business_error(request, code=40000, message="Invalid parameter", request_id="req-1")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BusinessApiError) as exc_info:
            _ = await client.get(TEST_PATH)

    assert exc_info.value.tiktok_code == 40000
    assert exc_info.value.request_id == "req-1"
    assert await get_posture("business-demo") == []


@pytest.mark.asyncio
async def test_no_refresh_token_auth_error_does_not_persist_broken() -> None:
    backend = MemoryBackend()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _business_error(request, code=40100, message="Invalid access token")

    async with _client(httpx.MockTransport(handler), refresh_token=None, backend=backend) as client:
        with pytest.raises(AccountBrokenError) as exc_info:
            _ = await client.get(TEST_PATH)

    assert paths == [TEST_PATH]
    assert exc_info.value.context["re_auth_required"] is True
    assert exc_info.value.context["tiktok_code"] == 40100
    key = account_key(ApiType.BUSINESS_ORGANIC, sandbox=True, alias="business-demo")
    assert key not in backend.values


@pytest.mark.asyncio
async def test_auth_error_with_refresh_token_refreshes_and_retries_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = MemoryBackend()
    seen_requests: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(
            (request.method, request.url.path, request.headers.get("Access-Token"))
        )
        if request.url.path == TEST_PATH and len(seen_requests) == 1:
            return _business_error(request, code=40105, message="Token expired")
        if request.url.path == BusinessAPIClient.REFRESH_PATH:
            refresh_body = cast(dict[str, object], json.loads(request.content.decode("utf-8")))
            assert refresh_body["refresh_token"] == "refresh-token-current"
            assert request.headers.get("Access-Token") is None
            return _business_response(
                request,
                {
                    "access_token": "access-token-refreshed",
                    "refresh_token": "refresh-token-refreshed",
                    "expires_in": 3600,
                    "refresh_expires_in": 7200,
                },
            )
        assert request.headers["Access-Token"] == "access-token-refreshed"
        return _business_response(request, {"advertiser_id": "adv-after-refresh"})

    async with _client(httpx.MockTransport(handler), backend=backend) as client:
        result = await client.get(TEST_PATH)

    assert result == {"advertiser_id": "adv-after-refresh"}
    assert seen_requests == [
        ("GET", TEST_PATH, "access-token-current"),
        ("POST", BusinessAPIClient.REFRESH_PATH, None),
        ("GET", TEST_PATH, "access-token-refreshed"),
    ]
    stored_account, stored_tokens = _stored_record(backend)
    assert stored_account.status is AccountStatus.OK
    assert stored_tokens.access_token.get_secret_value() == "access-token-refreshed"
    assert stored_tokens.refresh_token is not None
    assert stored_tokens.refresh_token.get_secret_value() == "refresh-token-refreshed"
    with caplog.at_level(logging.INFO):
        logging.getLogger().info(
            "refreshed %s %s",
            "access-token-refreshed",
            "refresh-token-refreshed",
        )
    assert "access-token-refreshed" not in caplog.text
    assert "refresh-token-refreshed" not in caplog.text


@pytest.mark.asyncio
async def test_second_auth_error_after_refresh_marks_account_broken() -> None:
    backend = MemoryBackend()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == BusinessAPIClient.REFRESH_PATH:
            return _business_response(
                request,
                {
                    "access_token": "access-token-refreshed",
                    "refresh_token": "refresh-token-refreshed",
                    "expires_in": 3600,
                },
            )
        return _business_error(request, code=40105, message="Token expired")

    async with _client(httpx.MockTransport(handler), backend=backend) as client:
        with pytest.raises(AccountBrokenError) as exc_info:
            _ = await client.get(TEST_PATH)
        assert client.account.status is AccountStatus.BROKEN

    stored_account, _stored_tokens = _stored_record(backend)
    assert stored_account.status is AccountStatus.BROKEN
    assert exc_info.value.context["re_auth_required"] is True


@pytest.mark.asyncio
async def test_429_records_retry_after_and_retries_idempotent_request() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                text="rate limited body must not surface",
                request=request,
            )
        return _business_response(request, {"ok": True})

    async with _client(httpx.MockTransport(handler)) as client:
        assert await client.get(TEST_PATH) == {"ok": True}

    posture = (await get_posture("business-demo"))[0]
    assert attempts == 2
    assert posture.last_retry_after_seconds == 0.0
    assert posture.last_429_at is not None
    assert posture.recent_request_count_last_60s == 1


@pytest.mark.asyncio
async def test_5xx_retries_idempotent_request() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                500,
                text="server body must not surface",
                request=request,
            )
        return _business_response(request, {"ok": True})

    async with _client(httpx.MockTransport(handler)) as client:
        assert await client.get(TEST_PATH) == {"ok": True}

    assert attempts == 2


@pytest.mark.asyncio
async def test_spike_s3_cassette_contract_compatible() -> None:
    cassette_path = Path("spikes/cassettes/s3_business_error.yaml")
    if not cassette_path.exists():
        pytest.skip("operator-recorded S3 cassette is not present yet")
    yaml = pytest.importorskip("yaml")
    interaction = _first_cassette_interaction(cassette_path, yaml)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == TEST_PATH
        return httpx.Response(
            interaction["status_code"],
            content=interaction["body"],
            headers=cast(dict[str, str], interaction["headers"]),
            request=request,
        )

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BusinessApiError) as exc_info:
            _ = await client.get(TEST_PATH, params={"advertiser_ids": json.dumps(["doesnotexist"])})

    assert exc_info.value.tiktok_code != 0
    assert exc_info.value.context["endpoint"] == TEST_PATH


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    refresh_token: str | None = "refresh-token-current",
    backend: MemoryBackend | None = None,
) -> BusinessAPIClient:
    return BusinessAPIClient(
        _account(refresh_token=refresh_token),
        _credentials(),
        backend=backend,
        transport=transport,
    )


def _account(*, refresh_token: str | None, sandbox: bool = True) -> AccountWithTokens:
    return AccountWithTokens(
        alias="business-demo",
        api_type=ApiType.BUSINESS_ORGANIC,
        sandbox=sandbox,
        tiktok_id="business-tiktok-id",
        display_name="Business Demo",
        avatar_url=None,
        scopes=["business.basic"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
        access_token=SecretStr("access-token-current"),
        refresh_token=SecretStr(refresh_token) if refresh_token is not None else None,
        access_token_expires_at=NOW + timedelta(minutes=5),
        refresh_token_expires_at=NOW + timedelta(days=30) if refresh_token else None,
        last_rotated_at=NOW,
    )


def _credentials(*, sandbox: bool = True) -> AppCredentials:
    return AppCredentials(
        api_type=ApiType.BUSINESS_ORGANIC,
        sandbox=sandbox,
        client_id=SecretStr("business-client-id"),
        client_secret=SecretStr("business-client-secret"),
        created_at=NOW,
    )


def _account_only(account: AccountWithTokens) -> Account:
    return Account(
        alias=account.alias,
        api_type=account.api_type,
        sandbox=account.sandbox,
        tiktok_id=account.tiktok_id,
        display_name=account.display_name,
        avatar_url=account.avatar_url,
        scopes=account.scopes,
        created_at=account.created_at,
        last_used_at=account.last_used_at,
        status=account.status,
    )


def _tokens_only(account: AccountWithTokens) -> AccountTokens:
    return AccountTokens(
        access_token=account.access_token,
        refresh_token=account.refresh_token,
        access_token_expires_at=account.access_token_expires_at,
        refresh_token_expires_at=account.refresh_token_expires_at,
        last_rotated_at=account.last_rotated_at,
    )


def _business_response(request: httpx.Request, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        httpx.codes.OK,
        json={"code": 0, "message": "OK", "request_id": "req-ok", "data": data},
        request=request,
    )


def _business_error(
    request: httpx.Request,
    *,
    code: int,
    message: str,
    request_id: str = "req-error",
) -> httpx.Response:
    return httpx.Response(
        httpx.codes.OK,
        json={"code": code, "message": message, "request_id": request_id},
        request=request,
    )


def _stored_record(backend: MemoryBackend) -> tuple[AccountWithTokens, AccountTokens]:
    key = account_key(ApiType.BUSINESS_ORGANIC, sandbox=True, alias="business-demo")
    stored = backend.values[key]
    account, tokens = deserialize_account_record(stored)
    account_with_tokens = AccountWithTokens(
        alias=account.alias,
        api_type=account.api_type,
        sandbox=account.sandbox,
        tiktok_id=account.tiktok_id,
        display_name=account.display_name,
        avatar_url=account.avatar_url,
        scopes=account.scopes,
        created_at=account.created_at,
        last_used_at=account.last_used_at,
        status=account.status,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_token_expires_at=tokens.access_token_expires_at,
        refresh_token_expires_at=tokens.refresh_token_expires_at,
        last_rotated_at=tokens.last_rotated_at,
    )
    return account_with_tokens, tokens


def _first_cassette_interaction(cassette_path: Path, yaml: Any) -> CassetteInteraction:
    payload = yaml.safe_load(cassette_path.read_text(encoding="utf-8"))
    interactions = payload["interactions"]
    response = interactions[0]["response"]
    body = response["body"].get("string", b"")
    body_bytes = body.encode("utf-8") if isinstance(body, str) else cast(bytes, body)
    return {
        "status_code": int(response["status"]["code"]),
        "headers": _single_value_headers(response.get("headers", {})),
        "body": body_bytes,
    }


def _single_value_headers(raw_headers: Mapping[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, list) and value:
            headers[str(key)] = str(value[0])
        elif value is not None:
            headers[str(key)] = str(value)
    return headers
