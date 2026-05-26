from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import keyring
import keyring.errors
import pytest
from jaraco.classes import properties
from keyring.backend import KeyringBackend as BaseKeyringBackend
from pydantic import SecretStr
from typing_extensions import override

import tiktok_mcp.api.display.client as display_client_module
import tiktok_mcp.auth.keychain as keychain_module
from tiktok_mcp.api.display.client import DISPLAY_BASE_URL, DisplayAPIClient
from tiktok_mcp.auth.http_sanitizer import install_httpx_sanitization
from tiktok_mcp.auth.keychain import (
    KeyringBackend,
    account_key,
    atomic_account_update,
    deserialize_account_record,
)
from tiktok_mcp.observability.rate_limit_tracker import get_posture, reset_tracker
from tiktok_mcp.types import Account, AccountBrokenError, AccountStatus, ApiType
from tiktok_mcp.types.accounts import AccountTokens
from tiktok_mcp.types.app_credentials import AppCredentials

SERVICE_NAME = "tiktok-mcp"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


class MemoryKeyring(BaseKeyringBackend):
    @properties.classproperty
    def priority(self) -> float:
        return 1

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[tuple[str, str], str] = {}

    @override
    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    @override
    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    @override
    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.values[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError("not found") from exc


@pytest.fixture(autouse=True)
def reset_display_client_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original_keyring = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    monkeypatch.setattr(keychain_module, "_backend", None)
    display_client_module._REFRESH_LOCKS.clear()
    reset_tracker()
    yield
    reset_tracker()
    display_client_module._REFRESH_LOCKS.clear()
    monkeypatch.setattr(keychain_module, "_backend", None)
    keyring.set_keyring(original_keyring)


@pytest.fixture
async def backend() -> KeyringBackend:
    selected_backend = await keychain_module.get_backend()
    assert isinstance(selected_backend, KeyringBackend)
    return selected_backend


@pytest.mark.asyncio
async def test_lazy_client_uses_bearer_authorization_and_json_post(
    backend: KeyringBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(alias="demo-display")
    await _store_account(backend, account, _make_tokens("fresh-access", "fresh-refresh"))
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, json={"data": {"ok": True}}, request=request)

    client = DisplayAPIClient(account, _make_credentials())
    _patch_http_client(monkeypatch, client, handler)

    assert client._client is None

    response = await client.request("POST", "/v2/user/info/", json={"fields": ["open_id"]})

    assert response == {"ok": True}
    assert client._client is not None
    assert seen_headers[0]["Authorization"] == "Bearer fresh-access"
    assert "Access-Token" not in seen_headers[0]
    assert seen_headers[0]["Content-Type"] == "application/json"
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_expired_token_requests_trigger_one_refresh(
    backend: KeyringBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(alias="expired-display")
    await _store_account(
        backend,
        account,
        _make_tokens("expired-access", "old-refresh", expires_at=datetime.now(UTC)),
    )
    refresh_requests: list[httpx.Request] = []
    api_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth/token/":
            refresh_requests.append(request)
            form = urllib.parse.parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["old-refresh"]
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                    "refresh_expires_in": 7200,
                },
                request=request,
            )
        api_authorizations.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": {"ok": True}}, request=request)

    client = DisplayAPIClient(account, _make_credentials())
    _patch_http_client(monkeypatch, client, handler)

    responses = await asyncio.gather(*(client.request("GET", "/v2/user/info/") for _ in range(5)))

    assert responses == [{"ok": True}] * 5
    assert len(refresh_requests) == 1
    assert api_authorizations == ["Bearer rotated-access"] * 5
    stored = await backend.get(account_key(ApiType.DISPLAY, False, "expired-display"))
    assert stored is not None
    _stored_account, stored_tokens = deserialize_account_record(stored)
    assert stored_tokens.access_token.get_secret_value() == "rotated-access"
    assert stored_tokens.refresh_token.get_secret_value() == "rotated-refresh"
    await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_retry_records_retry_after_and_success(
    backend: KeyringBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(alias="retry-display")
    await _store_account(backend, account, _make_tokens("fresh-access", "fresh-refresh"))
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                429,
                json={"error": {"code": "rate_limit_exceeded", "message": "slow down"}},
                headers={"Retry-After": "0", "x-tt-logid": f"retry-{attempts}"},
                request=request,
            )
        return httpx.Response(200, json={"data": {"ok": True}}, request=request)

    client = DisplayAPIClient(account, _make_credentials())
    _patch_http_client(monkeypatch, client, handler)

    response = await client.request("GET", "/v2/user/info/")

    assert response == {"ok": True}
    assert attempts == 3
    posture = (await get_posture("retry-display"))[0]
    assert posture.last_retry_after_seconds == 0.0
    assert posture.recent_request_count_last_60s == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_access_token_invalid_refreshes_and_retries_once(
    backend: KeyringBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(alias="invalid-display")
    await _store_account(backend, account, _make_tokens("stale-access", "refresh-one"))
    api_authorizations: list[str] = []
    refresh_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count
        if request.url.path == "/v2/oauth/token/":
            refresh_count += 1
            return httpx.Response(
                200,
                json={"access_token": "valid-access", "expires_in": 3600},
                request=request,
            )
        api_authorizations.append(request.headers["Authorization"])
        if len(api_authorizations) == 1:
            return httpx.Response(
                200,
                json={"error": {"code": "access_token_invalid", "message": "bad token"}},
                request=request,
            )
        return httpx.Response(200, json={"data": {"ok": True}}, request=request)

    client = DisplayAPIClient(account, _make_credentials())
    _patch_http_client(monkeypatch, client, handler)

    response = await client.request("GET", "/v2/user/info/")

    assert response == {"ok": True}
    assert refresh_count == 1
    assert api_authorizations == ["Bearer stale-access", "Bearer valid-access"]
    await client.aclose()


@pytest.mark.asyncio
async def test_second_invalid_access_token_marks_account_broken(
    backend: KeyringBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _make_account(alias="broken-display")
    await _store_account(backend, account, _make_tokens("stale-access", "refresh-one"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/oauth/token/":
            return httpx.Response(
                200,
                json={"access_token": "still-invalid-access", "expires_in": 3600},
                request=request,
            )
        if request.headers["Authorization"] == "Bearer stale-access":
            return httpx.Response(
                200,
                json={"error": {"code": "access_token_invalid", "message": "bad token"}},
                request=request,
            )
        return httpx.Response(
            401,
            json={"error": {"code": "access_token_invalid", "message": "still bad"}},
            headers={"x-tt-logid": "broken-log"},
            request=request,
        )

    client = DisplayAPIClient(account, _make_credentials())
    _patch_http_client(monkeypatch, client, handler)

    with pytest.raises(AccountBrokenError):
        _ = await client.request("GET", "/v2/user/info/")

    stored = await backend.get(account_key(ApiType.DISPLAY, False, "broken-display"))
    assert stored is not None
    stored_account, _stored_tokens = deserialize_account_record(stored)
    assert stored_account.status is AccountStatus.BROKEN
    await client.aclose()


def _patch_http_client(
    monkeypatch: pytest.MonkeyPatch,
    client: DisplayAPIClient,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def build_http_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=DISPLAY_BASE_URL,
            timeout=30.0,
        )

    monkeypatch.setattr(client, "_build_http_client", build_http_client)


def _make_account(alias: str) -> Account:
    return Account(
        alias=alias,
        api_type=ApiType.DISPLAY,
        sandbox=False,
        tiktok_id="test-open-id",
        display_name="Demo Display",
        avatar_url=None,
        scopes=["user.info.basic"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
    )


def _make_credentials() -> AppCredentials:
    return AppCredentials(
        api_type=ApiType.DISPLAY,
        sandbox=False,
        client_id=SecretStr("test-client-id"),
        client_secret=SecretStr("test-client-secret"),
        created_at=NOW,
    )


def _make_tokens(
    access_token: str,
    refresh_token: str,
    *,
    expires_at: datetime | None = None,
) -> AccountTokens:
    return AccountTokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token),
        access_token_expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
        last_rotated_at=datetime.now(UTC),
    )


async def _store_account(
    backend: KeyringBackend,
    account: Account,
    tokens: AccountTokens,
) -> None:
    await atomic_account_update(
        backend,
        account.api_type,
        account.sandbox,
        account.alias,
        account,
        tokens,
    )


def test_httpx_sanitization_hook_is_available() -> None:
    client = httpx.AsyncClient(base_url=DISPLAY_BASE_URL)
    try:
        install_httpx_sanitization(client)

        hook_names = [getattr(hook, "__qualname__", "") for hook in client.event_hooks["response"]]
        assert any("safe_raise_for_status" in hook_name for hook_name in hook_names)
    finally:
        asyncio.run(client.aclose())
