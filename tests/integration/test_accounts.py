from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import keyring
import keyring.errors
import pytest
from freezegun import freeze_time
from jaraco.classes import properties
from keyring.backend import KeyringBackend as BaseKeyringBackend
from pydantic import SecretStr
from typing_extensions import override

import tiktok_mcp.auth.keychain as keychain_module
import tiktok_mcp.tools.accounts as accounts_module
from tiktok_mcp.auth.keychain import (
    KeyringBackend,
    account_key,
    app_creds_key,
    atomic_account_update,
    deserialize_account_record,
)
from tiktok_mcp.auth.state import create_state, reset_state_manager
from tiktok_mcp.tools.accounts import (
    LOOPBACK_INSTRUCTIONS,
    add_account,
    add_account_with_loopback,
    complete_account_login,
    list_accounts,
    poll_loopback_login,
    remove_account,
    rename_account,
)
from tiktok_mcp.tools.app_credentials import set_app_credentials
from tiktok_mcp.types.accounts import (
    MARKETING_DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
    Account,
    AccountStatus,
    AccountTokens,
    ApiType,
)

SERVICE_NAME = "tiktok-mcp"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
REDIRECT_URI = "https://oauth.example.com/tiktok/oauth/callback"
TOKEN_PAYLOAD: dict[str, object] = {
    "access" + "_token": "synthetic-access-token",
    "refresh" + "_token": "synthetic-refresh-token",
    "expires_in": 86400,
    "scope": "user.info.basic",
    "token_type": "Bearer",
    "open_id": "test-open-id",
    "refresh_expires_in": 31536000,
}
BUSINESS_TOKEN_PAYLOAD: dict[str, object] = {
    "access" + "_token": "synthetic-business-access-token",
    "refresh" + "_token": "synthetic-business-refresh-token",
    "expires_in": 86400,
    "scope": "ad.manage",
    "token_type": "Bearer",
    "advertiser_ids": ["test-advertiser-id"],
    "refresh_expires_in": 31536000,
}
MARKETING_TOKEN_PAYLOAD_WITHOUT_EXPIRES: dict[str, object] = {
    "access" + "_token": "synthetic-marketing-access-token",
    "refresh" + "_token": "synthetic-marketing-refresh-token",
    "refresh_token_expire_in": 31536000,
    "advertiser_ids": ["test-advertiser-id"],
    "scope": ["ad.manage", "report.read"],
}
ORGANIC_TOKEN_PAYLOAD: dict[str, object] = {
    "access" + "_token": "synthetic-organic-access-token",
    "refresh" + "_token": "synthetic-organic-refresh-token",
    "expires_in": 86400,
    "scope": "user.info.basic,video.list,comment.list,comment.list.manage",
    "token_type": "Bearer",
    "open_id": "organic-open-id",
    "refresh_token_expires_in": 31536000,
}


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
def reset_account_tool_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original_keyring = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    monkeypatch.setattr(keychain_module, "_backend", None)
    reset_state_manager()
    accounts_module._PENDING_REMOVALS.clear()
    accounts_module._PENDING_LOOPBACK_LOGINS.clear()
    yield
    for pending_login in accounts_module._PENDING_LOOPBACK_LOGINS.values():
        pending_login.task.cancel()
        pending_login.server.close()
    accounts_module._PENDING_LOOPBACK_LOGINS.clear()
    accounts_module._PENDING_REMOVALS.clear()
    reset_state_manager()
    monkeypatch.setattr(keychain_module, "_backend", None)
    keyring.set_keyring(original_keyring)


@pytest.fixture
async def backend() -> KeyringBackend:
    selected_backend = await keychain_module.get_backend()
    assert isinstance(selected_backend, KeyringBackend)
    return selected_backend


@pytest.fixture
def allow_account_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")


@pytest.mark.asyncio
async def test_add_account_returns_url_with_state_and_alias(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    """add_account returns an authorization URL, state, alias, and 600s expiry."""
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY)

    response = await add_account(ApiType.DISPLAY)

    assert set(response) == {"url", "state", "suggested_alias", "expires_in", "instructions"}
    assert response["expires_in"] == 600
    assert response["suggested_alias"].startswith("nordic-display-")
    assert "Incorrect parameters" in response["instructions"]
    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    assert parsed_url.geturl().startswith("https://www.tiktok.com/v2/auth/authorize/")
    assert params["state"] == [response["state"]]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["code_challenge_method"] == ["S256"]
    assert "code_challenge" in params


@pytest.mark.asyncio
async def test_add_account_sandbox_loads_sandbox_creds(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        sandbox=True,
        client_id="sandbox-client-id",
    )

    response = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-sandbox",
        sandbox=True,
    )

    assert "error" not in response
    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    assert params["client_key"] == ["sandbox-client-id"]
    assert params["state"] == [response["state"]]


@pytest.mark.asyncio
async def test_add_account_business_organic_uses_tiktok_account_holder_oauth(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.BUSINESS_ORGANIC,
        client_id="organic-client-id",
    )

    response = await add_account(ApiType.BUSINESS_ORGANIC, alias="nordic-comments-oauth")

    assert "error" not in response
    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    scopes = set(params["scope"][0].split(","))
    assert parsed_url.scheme == "https"
    assert parsed_url.netloc == "www.tiktok.com"
    assert parsed_url.path == "/v2/auth/authorize/"
    assert params["client_key"] == ["organic-client-id"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["state"] == [response["state"]]
    assert {"user.info.basic", "video.list", "comment.list", "comment.list.manage"} <= scopes
    assert "app_id" not in params
    assert "code_challenge" not in params


@pytest.mark.asyncio
async def test_set_app_credentials_redirect_uri_enables_add_account(
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display-client-from-tool",
        client_secret="display-secret-from-tool",
        redirect_uri="http://localhost:8000/callback",
        sandbox=True,
    )

    response = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-from-tool",
        sandbox=True,
    )

    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    assert params["client_key"] == ["display-client-from-tool"]
    assert params["redirect_uri"] == ["http://localhost:8000/callback"]


@pytest.mark.asyncio
async def test_add_account_reports_redirect_uri_not_set_for_legacy_display_credentials(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials_without_redirect_uri(backend, ApiType.DISPLAY)

    response = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-legacy",
    )

    assert response["error"] == "redirect_uri_not_set"
    assert response["context"] == {"api_type": "display", "sandbox": False}


@pytest.mark.asyncio
async def test_add_account_reports_redirect_uri_not_set_for_legacy_content_posting_credentials(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials_without_redirect_uri(backend, ApiType.CONTENT_POSTING)

    response = await add_account(
        ApiType.CONTENT_POSTING,
        alias="nordic-posting-legacy",
    )

    assert response["error"] == "redirect_uri_not_set"
    assert response["context"] == {"api_type": "content_posting", "sandbox": False}


@pytest.mark.asyncio
async def test_add_account_reports_redirect_uri_not_set_for_legacy_business_organic_credentials(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials_without_redirect_uri(backend, ApiType.BUSINESS_ORGANIC)

    response = await add_account(
        ApiType.BUSINESS_ORGANIC,
        alias="nordic-organic-legacy",
    )

    assert response["error"] == "redirect_uri_not_set"
    assert response["context"] == {"api_type": "business_organic", "sandbox": False}


@pytest.mark.asyncio
async def test_add_account_reports_redirect_uri_not_set_for_legacy_marketing_credentials(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials_without_redirect_uri(backend, ApiType.MARKETING)

    response = await add_account(
        ApiType.MARKETING,
        alias="nordic-marketing-legacy",
    )

    assert response["error"] == "redirect_uri_not_set"
    assert response["context"] == {"api_type": "marketing", "sandbox": False}


@pytest.mark.asyncio
async def test_add_account_uses_tiktok_desktop_hex_pkce_challenge(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    pkce_verifier = "A" * 43
    await _store_app_credentials(backend, ApiType.DISPLAY)
    monkeypatch.setattr(accounts_module, "_new_pkce_verifier", lambda: pkce_verifier)

    response = await add_account(ApiType.DISPLAY, alias="nordic-display-pkce")

    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    expected_challenge = hashlib.sha256(pkce_verifier.encode("ascii")).hexdigest()

    assert len(pkce_verifier) == 43
    assert params["code_challenge"] == [expected_challenge]
    assert params["code_challenge_method"] == ["S256"]


@pytest.mark.asyncio
async def test_add_account_production_default(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        sandbox=False,
        client_id="production-client-id",
    )
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        sandbox=True,
        client_id="sandbox-client-id",
    )

    response = await add_account(ApiType.DISPLAY, alias="nordic-display-prod")

    assert "error" not in response
    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    assert params["client_key"] == ["production-client-id"]


@pytest.mark.asyncio
async def test_add_account_blocked_when_account_changes_disabled() -> None:
    """add_account returns a structured error when the env gate is disabled."""
    response = await add_account(ApiType.DISPLAY)

    assert response["error"] == "account_changes_disabled"
    assert response["tool"] == "add_account"


@pytest.mark.asyncio
async def test_complete_account_login_validates_state(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    """complete_account_login reports unknown state before token exchange."""
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY)

    response = await complete_account_login(_redirect_url("code-123", "never-created"))

    assert response["error"] == "oauth_state_invalid"
    assert response["context"]["reason"] == "unknown"


@pytest.mark.asyncio
async def test_complete_account_login_host_mismatch(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    """complete_account_login rejects redirects from unregistered hosts."""
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY)
    oauth_state = await create_state(ApiType.DISPLAY, "nordic-display-host")
    bad_redirect = "https://evil.example/callback?code=code-123&state=" + oauth_state.state

    response = await complete_account_login(bad_redirect)

    assert response["error"] == "oauth_host_mismatch"
    assert response["context"]["expected_host"] == "oauth.example.com"
    assert response["context"]["actual_host"] == "evil.example"


@pytest.mark.asyncio
async def test_complete_account_login_happy_path(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """complete_account_login exchanges a synthetic token and stores an account."""
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY)
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    add_response = await add_account(ApiType.DISPLAY, alias="nordic-display-good")
    redirect = _redirect_url("synthetic-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["alias"] == "nordic-display-good"
    assert response["api_type"] == "display"
    assert response["sandbox"] is False
    assert "access_token" not in response
    assert "refresh_token" not in response
    stored = await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-good"))
    assert stored is not None
    account, _tokens = deserialize_account_record(stored)
    assert account.alias == "nordic-display-good"


@pytest.mark.asyncio
async def test_complete_account_login_allows_missing_refresh_token(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    token_payload_without_refresh = dict(TOKEN_PAYLOAD)
    token_payload_without_refresh.pop("refresh_token")
    token_payload_without_refresh.pop("refresh_expires_in")
    await _store_app_credentials(backend, ApiType.DISPLAY)
    _mock_token_exchange(monkeypatch, token_payload_without_refresh)
    add_response = await add_account(ApiType.DISPLAY, alias="nordic-display-no-refresh")
    redirect = _redirect_url("synthetic-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["alias"] == "nordic-display-no-refresh"
    stored = await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-no-refresh"))
    assert stored is not None
    _account, tokens = deserialize_account_record(stored)
    assert tokens.refresh_token is None
    assert tokens.refresh_token_expires_at is None


@pytest.mark.asyncio
async def test_oauth_pkce_token_exchange_success(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    pkce_verifier = "A" * 43
    await _store_app_credentials(backend, ApiType.DISPLAY)

    def assert_pkce_request(request: httpx.Request) -> None:
        body = urllib.parse.parse_qs(request.content.decode())
        assert body["code_verifier"] == [pkce_verifier]
        assert body["grant_type"] == ["authorization_code"]
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"

    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD, assert_request=assert_pkce_request)
    oauth_state = await create_state(
        ApiType.DISPLAY,
        "nordic-display-pkce",
        pkce_verifier=pkce_verifier,
    )

    response = await complete_account_login(_redirect_url("synthetic-code", oauth_state.state))

    assert response["alias"] == "nordic-display-pkce"
    assert response["api_type"] == "display"


@pytest.mark.asyncio
async def test_business_organic_token_exchange_uses_tt_user_endpoint(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.BUSINESS_ORGANIC)
    _mock_token_exchange(
        monkeypatch,
        {"code": 0, "data": ORGANIC_TOKEN_PAYLOAD},
        assert_request=_assert_business_organic_oauth_request,
    )
    add_response = await add_account(ApiType.BUSINESS_ORGANIC, alias="nordic-comments-token")
    redirect = _redirect_url("synthetic-organic-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["alias"] == "nordic-comments-token"
    assert response["api_type"] == "business_organic"
    assert response["tiktok_id_fingerprint"] == "orga...len=15"
    stored = await backend.get(
        account_key(ApiType.BUSINESS_ORGANIC, False, "nordic-comments-token")
    )
    assert stored is not None
    account, tokens = deserialize_account_record(stored)
    assert account.tiktok_id == "organic-open-id"
    assert account.scopes == [
        "user.info.basic",
        "video.list",
        "comment.list",
        "comment.list.manage",
    ]
    assert tokens.refresh_token_expires_at is not None


@pytest.mark.asyncio
async def test_business_organic_sandbox_token_exchange_uses_business_oauth_host(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.BUSINESS_ORGANIC, sandbox=True)
    _mock_token_exchange(
        monkeypatch,
        {"code": 0, "data": ORGANIC_TOKEN_PAYLOAD},
        assert_request=_assert_business_organic_oauth_request,
    )
    add_response = await add_account(
        ApiType.BUSINESS_ORGANIC,
        alias="nordic-comments-sandbox-token",
        sandbox=True,
    )
    redirect = _redirect_url("synthetic-organic-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["alias"] == "nordic-comments-sandbox-token"
    assert response["api_type"] == "business_organic"
    assert response["sandbox"] is True
    assert await backend.get(
        account_key(ApiType.BUSINESS_ORGANIC, True, "nordic-comments-sandbox-token")
    )


@pytest.mark.asyncio
async def test_marketing_token_exchange_accepts_response_without_expires_in(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.MARKETING)
    _mock_token_exchange(
        monkeypatch,
        {"code": 0, "message": "OK", "data": MARKETING_TOKEN_PAYLOAD_WITHOUT_EXPIRES},
        assert_request=_assert_marketing_oauth_request,
    )
    add_response = await add_account(ApiType.MARKETING, alias="nordic-marketing-token")
    redirect = _redirect_url("synthetic-business-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["alias"] == "nordic-marketing-token"
    assert response["api_type"] == "marketing"
    assert response["tiktok_id_fingerprint"] == "test...len=18"
    stored = await backend.get(account_key(ApiType.MARKETING, False, "nordic-marketing-token"))
    assert stored is not None
    account, tokens = deserialize_account_record(stored)
    assert account.scopes == ["ad.manage", "report.read"]
    assert tokens.access_token_expires_at - tokens.last_rotated_at == timedelta(
        seconds=MARKETING_DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    )
    assert tokens.refresh_token_expires_at is not None


@pytest.mark.asyncio
async def test_marketing_account_appears_in_list_accounts_after_token_exchange(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.MARKETING)
    _mock_token_exchange(
        monkeypatch,
        {"code": 0, "message": "OK", "data": MARKETING_TOKEN_PAYLOAD_WITHOUT_EXPIRES},
        assert_request=_assert_marketing_oauth_request,
    )
    add_response = await add_account(ApiType.MARKETING, alias="nordic-marketing-list")
    redirect = _redirect_url("synthetic-business-code", str(add_response["state"]))
    _ = await complete_account_login(redirect)

    response = await list_accounts()

    assert response["count"] == 1
    assert response["accounts"][0]["alias"] == "nordic-marketing-list"
    assert response["accounts"][0]["api_type"] == "marketing"


@pytest.mark.asyncio
async def test_display_token_exchange_still_requires_expires_in(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    display_payload_without_expires = dict(TOKEN_PAYLOAD)
    display_payload_without_expires.pop("expires_in")
    await _store_app_credentials(backend, ApiType.DISPLAY)
    _mock_token_exchange(monkeypatch, display_payload_without_expires)
    add_response = await add_account(ApiType.DISPLAY, alias="nordic-display-no-expiry")
    redirect = _redirect_url("synthetic-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["error"] == "invalid_redirect_url"
    assert response["message"] == "Token exchange response is missing integer field expires_in."


@pytest.mark.asyncio
async def test_oauth_error_envelope_surfaced(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY)
    _mock_token_exchange(
        monkeypatch,
        {
            "error": "invalid_request",
            "error_description": "Code verifier or code challenge is invalid.",
            "log_id": "oauth-log-id",
        },
    )
    add_response = await add_account(ApiType.DISPLAY, alias="nordic-display-error")
    redirect = _redirect_url("synthetic-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["error"] == "token_exchange_failed"
    assert response["message"] == "Code verifier or code challenge is invalid."
    assert response["context"]["tiktok_error"] == "invalid_request"
    assert response["context"]["log_id"] == "oauth-log-id"
    assert "missing string field access_token" not in json.dumps(response)


def _install_loopback_browser(
    monkeypatch: pytest.MonkeyPatch,
    *,
    callback_code: str = "synthetic-code",
    callback_state: str | None = None,
    expected_status: int = httpx.codes.OK,
) -> list[str]:
    opened_urls: list[str] = []

    def open_browser(url: str) -> bool:
        opened_urls.append(url)

        async def hit_callback() -> None:
            parsed_url = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed_url.query)
            returned_state = callback_state or params["state"][0]
            redirect_uri = urllib.parse.urlparse(params["redirect_uri"][0])
            callback_query = urllib.parse.urlencode(
                {"code": callback_code, "state": returned_state}
            )
            callback_url = (
                f"http://127.0.0.1:{redirect_uri.port}{redirect_uri.path}?{callback_query}"
            )
            async with httpx.AsyncClient() as client:
                callback_response = await client.get(callback_url)
            assert callback_response.status_code == expected_status
            if expected_status == httpx.codes.OK:
                assert "Authentication complete" in callback_response.text
            else:
                assert "Authentication failed" in callback_response.text

        _ = asyncio.create_task(hit_callback())
        return True

    monkeypatch.setattr("tiktok_mcp.tools.accounts.webbrowser.open", open_browser)
    return opened_urls


def _install_loopback_browser_no_callback(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened_urls: list[str] = []

    def open_browser(url: str) -> bool:
        opened_urls.append(url)
        return True

    monkeypatch.setattr("tiktok_mcp.tools.accounts.webbrowser.open", open_browser)
    return opened_urls


async def _hit_loopback_callback(
    authorization_url: str,
    *,
    callback_code: str = "synthetic-code",
    callback_state: str | None = None,
    expected_status: int = httpx.codes.OK,
) -> httpx.Response:
    parsed_url = urllib.parse.urlparse(authorization_url)
    params = urllib.parse.parse_qs(parsed_url.query)
    returned_state = callback_state or params["state"][0]
    redirect_uri = urllib.parse.urlparse(params["redirect_uri"][0])
    callback_query = urllib.parse.urlencode({"code": callback_code, "state": returned_state})
    callback_url = f"http://127.0.0.1:{redirect_uri.port}{redirect_uri.path}?{callback_query}"
    async with httpx.AsyncClient() as client:
        callback_response = await client.get(callback_url)
    assert callback_response.status_code == expected_status
    return callback_response


@pytest.mark.asyncio
async def test_add_account_with_loopback_display_happy_path(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        redirect_uri=f"http://localhost:{unused_tcp_port}/callback",
    )
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    pkce_verifier = "A" * 43
    monkeypatch.setattr(accounts_module, "_new_pkce_verifier", lambda: pkce_verifier)
    opened_urls = _install_loopback_browser(monkeypatch)

    response = await add_account_with_loopback(
        ApiType.DISPLAY,
        alias="nordic-display-loopback",
        scopes=["user.info.basic", "video.list"],
    )
    poll_response = await poll_loopback_login(str(response["state"]), wait_seconds=5)

    parsed_auth_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_auth_url.query)
    redirect_uri = urllib.parse.urlparse(params["redirect_uri"][0])
    expected_challenge = hashlib.sha256(pkce_verifier.encode("ascii")).hexdigest()

    assert response["status"] == "pending"
    assert response["poll_with"] == "poll_loopback_login"
    assert opened_urls == [response["url"]]
    assert poll_response["alias"] == "nordic-display-loopback"
    assert poll_response["api_type"] == "display"
    assert response["instructions"] == LOOPBACK_INSTRUCTIONS
    assert params["scope"] == ["user.info.basic,video.list"]
    assert params["code_challenge"] == [expected_challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert redirect_uri.hostname == "localhost"
    assert redirect_uri.port == unused_tcp_port
    assert await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-loopback"))


@pytest.mark.asyncio
async def test_add_account_with_loopback_business_happy_path(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.MARKETING,
        sandbox=True,
        client_id="sandbox-client-id",
        redirect_uri=f"http://localhost:{unused_tcp_port}/callback",
    )
    _mock_token_exchange(
        monkeypatch,
        {"code": 0, "data": BUSINESS_TOKEN_PAYLOAD},
        assert_request=_assert_business_oauth_request,
    )
    opened_urls = _install_loopback_browser(monkeypatch, callback_code="synthetic-business-code")

    response = await add_account_with_loopback(
        ApiType.MARKETING,
        sandbox=True,
        alias="nordic-marketing-loopback",
        scopes=["ignored-scope"],
    )
    poll_response = await poll_loopback_login(str(response["state"]), wait_seconds=5)

    parsed_auth_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_auth_url.query)

    assert response["status"] == "pending"
    assert opened_urls == [response["url"]]
    assert poll_response["alias"] == "nordic-marketing-loopback"
    assert poll_response["api_type"] == "marketing"
    assert poll_response["sandbox"] is True
    assert response["instructions"] == LOOPBACK_INSTRUCTIONS
    assert parsed_auth_url.netloc == "business-api.tiktok.com"
    assert parsed_auth_url.path == "/portal/auth"
    assert params["app_id"] == ["sandbox-client-id"]
    assert params["redirect_uri"] == [f"http://localhost:{unused_tcp_port}/callback"]
    assert "scope" not in params
    assert "code_challenge" not in params
    assert await backend.get(account_key(ApiType.MARKETING, True, "nordic-marketing-loopback"))


@pytest.mark.asyncio
async def test_add_account_with_loopback_can_use_dynamic_port(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend, ApiType.DISPLAY, redirect_uri="http://localhost:8000/callback"
    )
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    opened_urls = _install_loopback_browser(monkeypatch)

    response = await add_account_with_loopback(
        ApiType.DISPLAY,
        alias="nordic-display-dynamic",
        callback_port=0,
    )
    poll_response = await poll_loopback_login(str(response["state"]), wait_seconds=5)

    parsed_auth_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_auth_url.query)
    redirect_uri = urllib.parse.urlparse(params["redirect_uri"][0])

    assert opened_urls == [response["url"]]
    assert poll_response["alias"] == "nordic-display-dynamic"
    assert redirect_uri.hostname == "localhost"
    assert redirect_uri.port is not None
    assert await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-dynamic"))


@pytest.mark.asyncio
async def test_add_account_with_loopback_rejects_state_mismatch_before_exchange(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        redirect_uri=f"http://localhost:{unused_tcp_port}/callback",
    )
    opened_urls = _install_loopback_browser(
        monkeypatch,
        callback_state="attacker-state",
        expected_status=httpx.codes.BAD_REQUEST,
    )

    def fail_token_exchange() -> httpx.AsyncClient:
        raise AssertionError("state-mismatched callback must not exchange tokens")

    monkeypatch.setattr(accounts_module, "_build_http_client", fail_token_exchange)

    response = await add_account_with_loopback(
        ApiType.DISPLAY,
        alias="nordic-display-badstate",
    )
    poll_response = await poll_loopback_login(str(response["state"]), wait_seconds=5)

    assert len(opened_urls) == 1
    assert poll_response["error"] == "oauth_state_invalid"
    assert poll_response["context"]["reason"] == "unknown"
    assert await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-badstate")) is None


@pytest.mark.asyncio
async def test_add_account_with_loopback_rejects_invalid_callback_port(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend, ApiType.DISPLAY, redirect_uri="http://localhost:8000/callback"
    )

    response = await add_account_with_loopback(
        ApiType.DISPLAY,
        alias="nordic-display-bad-port",
        callback_port=70000,
    )

    assert response["error"] == "invalid_callback_port"


@pytest.mark.asyncio
async def test_add_account_with_loopback_falls_back_to_manual_url_when_port_busy(
    backend: KeyringBackend,
    allow_account_changes: None,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    redirect_uri = f"http://localhost:{unused_tcp_port}/callback"
    await _store_app_credentials(backend, ApiType.DISPLAY, redirect_uri=redirect_uri)
    server = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", unused_tcp_port)

    try:
        response = await add_account_with_loopback(
            ApiType.DISPLAY,
            alias="nordic-display-busy-port",
        )
    finally:
        server.close()
        await server.wait_closed()

    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    assert response["warning"] == "oauth_loopback_unavailable"
    assert response["suggested_alias"] == "nordic-display-busy-port"
    assert params["redirect_uri"] == [redirect_uri]


def _assert_business_oauth_request(request: httpx.Request) -> None:
    assert request.url.host == "business-api.tiktok.com"
    assert request.url.path == "/open_api/v1.3/oauth2/access_token/"
    assert request.headers["Content-Type"].startswith("application/json")
    payload = json.loads(request.content.decode())
    assert payload == {
        "app_id": "sandbox-client-id",
        "secret": "test-client-secret",
        "auth_code": "synthetic-business-code",
    }


def _assert_marketing_oauth_request(request: httpx.Request) -> None:
    assert request.url.host == "business-api.tiktok.com"
    assert request.url.path == "/open_api/v1.3/oauth2/access_token/"
    assert request.headers["Content-Type"].startswith("application/json")
    payload = json.loads(request.content.decode())
    assert payload == {
        "app_id": "test-client-id",
        "secret": "test-client-secret",
        "auth_code": "synthetic-business-code",
    }


def _assert_business_organic_oauth_request(request: httpx.Request) -> None:
    assert request.url.host == "business-api.tiktok.com"
    assert request.url.path == "/open_api/v1.3/tt_user/oauth2/token/"
    assert request.headers["Content-Type"].startswith("application/json")
    payload = json.loads(request.content.decode())
    assert payload == {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "grant_type": "authorization_code",
        "auth_code": "synthetic-organic-code",
        "redirect_uri": REDIRECT_URI,
    }


@pytest.mark.asyncio
async def test_loopback_callback_capture(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    redirect_uri = f"http://localhost:{unused_tcp_port}/callback"
    await _store_app_credentials(backend, ApiType.DISPLAY, redirect_uri=redirect_uri)
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    opened_urls: list[str] = []

    def open_browser(url: str) -> bool:
        opened_urls.append(url)

        async def hit_callback() -> None:
            parsed_url = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed_url.query)
            callback_query = urllib.parse.urlencode(
                {"code": "synthetic-code", "state": params["state"][0]}
            )
            callback_url = f"http://127.0.0.1:{unused_tcp_port}/callback?{callback_query}"
            async with httpx.AsyncClient() as client:
                callback_response = await client.get(callback_url)
            assert callback_response.status_code == httpx.codes.OK
            assert "Authentication complete" in callback_response.text

        _ = asyncio.create_task(hit_callback())
        return True

    monkeypatch.setattr("tiktok_mcp.tools.accounts.webbrowser.open", open_browser)

    response = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-loopback",
        await_callback=True,
    )
    poll_response = await poll_loopback_login(str(response["state"]), wait_seconds=5)

    assert opened_urls
    assert response["status"] == "pending"
    assert poll_response["alias"] == "nordic-display-loopback"
    assert poll_response["api_type"] == "display"
    assert await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-loopback"))


@pytest.mark.asyncio
async def test_poll_loopback_login_returns_pending_before_callback(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        redirect_uri=f"http://localhost:{unused_tcp_port}/callback",
    )
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    opened_urls = _install_loopback_browser_no_callback(monkeypatch)

    response = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-pending",
        await_callback=True,
    )
    pending_response = await poll_loopback_login(str(response["state"]))
    callback_response = await _hit_loopback_callback(str(response["url"]))
    complete_response = await poll_loopback_login(str(response["state"]), wait_seconds=5)

    assert opened_urls == [response["url"]]
    assert pending_response["status"] == "pending"
    assert "Authentication complete" in callback_response.text
    assert complete_response["alias"] == "nordic-display-pending"


@pytest.mark.asyncio
async def test_loopback_timeout_is_reported_by_poll(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        redirect_uri=f"http://localhost:{unused_tcp_port}/callback",
    )
    monkeypatch.setattr(accounts_module, "_LOOPBACK_TIMEOUT_SECONDS", 0.01)
    opened_urls = _install_loopback_browser_no_callback(monkeypatch)

    response = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-timeout",
        await_callback=True,
    )
    poll_response = await poll_loopback_login(str(response["state"]), wait_seconds=1)

    assert opened_urls == [response["url"]]
    assert poll_response["error"] == "oauth_loopback_timeout"
    assert poll_response["context"]["timeout_seconds"] == 0.01


@pytest.mark.asyncio
async def test_second_pending_loopback_on_same_port_is_rejected(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    redirect_uri = f"http://localhost:{unused_tcp_port}/callback"
    await _store_app_credentials(backend, ApiType.DISPLAY, redirect_uri=redirect_uri)
    await _store_app_credentials(backend, ApiType.CONTENT_POSTING, redirect_uri=redirect_uri)
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    _ = _install_loopback_browser_no_callback(monkeypatch)

    first = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-first",
        await_callback=True,
    )
    second = await add_account(
        ApiType.CONTENT_POSTING,
        alias="nordic-posting-second",
        await_callback=True,
    )
    await _hit_loopback_callback(str(first["url"]))
    complete_response = await poll_loopback_login(str(first["state"]), wait_seconds=5)

    assert first["status"] == "pending"
    assert second["error"] == "oauth_loopback_already_pending"
    assert second["state"] == first["state"]
    assert second["poll_with"] == "poll_loopback_login"
    assert complete_response["alias"] == "nordic-display-first"


@pytest.mark.asyncio
async def test_add_account_preserves_manual_fallback_for_loopback_redirect(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.DISPLAY,
        redirect_uri=f"http://localhost:{unused_tcp_port}/callback",
    )

    def fail_if_opened(url: str) -> bool:
        raise AssertionError(f"manual add_account should not open browser: {url}")

    monkeypatch.setattr("tiktok_mcp.tools.accounts.webbrowser.open", fail_if_opened)

    response = await add_account(ApiType.DISPLAY, alias="nordic-display-manual")

    assert set(response) == {"url", "state", "suggested_alias", "expires_in", "instructions"}
    assert response["suggested_alias"] == "nordic-display-manual"
    parsed_url = urllib.parse.urlparse(response["url"])
    params = urllib.parse.parse_qs(parsed_url.query)
    assert params["redirect_uri"] == [f"http://localhost:{unused_tcp_port}/callback"]


@pytest.mark.asyncio
async def test_complete_account_login_persists_sandbox_flag(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY, sandbox=True)
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    add_response = await add_account(
        ApiType.DISPLAY,
        alias="nordic-display-sandbox",
        sandbox=True,
    )
    redirect = _redirect_url("synthetic-code", str(add_response["state"]))

    response = await complete_account_login(redirect)

    assert response["alias"] == "nordic-display-sandbox"
    assert response["sandbox"] is True
    sandbox_record = await backend.get(account_key(ApiType.DISPLAY, True, "nordic-display-sandbox"))
    assert sandbox_record is not None
    assert await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-sandbox")) is None
    account, _tokens = deserialize_account_record(sandbox_record)
    assert account.sandbox is True


@pytest.mark.parametrize(
    "url_shape",
    (
        "{url}",
        "  {url}  ",
        '"{url}"',
        "`{url}`",
        "[TikTok redirect]({url})",
        "{url}\n",
    ),
)
@pytest.mark.asyncio
async def test_complete_account_login_url_robustness(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
    url_shape: str,
) -> None:
    """complete_account_login accepts all six spike-validated paste shapes."""
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY)
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    add_response = await add_account(ApiType.DISPLAY, alias="nordic-display-shape")
    redirect = _redirect_url("synthetic-code", str(add_response["state"]))

    response = await complete_account_login(url_shape.format(url=redirect))

    assert response["alias"] == "nordic-display-shape"
    assert response["tiktok_id_fingerprint"] == "test...len=12"


@pytest.mark.asyncio
async def test_list_accounts_returns_summary_without_secrets(
    backend: KeyringBackend,
) -> None:
    """list_accounts returns sanitized summaries for stored accounts."""
    await _store_account(backend, alias="nordic-display-one", raw_id="raw-tiktok-id-one")
    await _store_account(backend, alias="nordic-display-two", raw_id="raw-tiktok-id-two")

    response = await list_accounts()

    assert response["count"] == 2
    serialized = json.dumps(response, sort_keys=True)
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert "client_secret" not in serialized
    assert "raw-tiktok-id-one" not in serialized
    assert "raw-tiktok-id-two" not in serialized


@pytest.mark.asyncio
async def test_rename_account_atomic(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    """rename_account moves the record and preserves the account fingerprint."""
    _ = allow_account_changes
    await _store_account(backend, alias="old-alias", raw_id="same-fingerprint-id")

    response = await rename_account("old-alias", "new-alias")
    listed = await list_accounts()

    assert response["alias"] == "new-alias"
    assert response["tiktok_id_fingerprint"] == "same...len=19"
    aliases = {account["alias"] for account in listed["accounts"]}
    assert "old-alias" not in aliases
    assert "new-alias" in aliases
    assert await backend.get(account_key(ApiType.DISPLAY, False, "old-alias")) is None


@pytest.mark.asyncio
async def test_rename_account_sandbox_uses_selected_namespace(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_account(backend, alias="shared-alias", raw_id="production-id")
    await _store_account(backend, alias="shared-alias", raw_id="sandbox-id", sandbox=True)

    response = await rename_account("shared-alias", "sandbox-renamed", sandbox=True)

    assert response["alias"] == "sandbox-renamed"
    assert response["sandbox"] is True
    assert await backend.get(account_key(ApiType.DISPLAY, False, "shared-alias")) is not None
    assert await backend.get(account_key(ApiType.DISPLAY, True, "shared-alias")) is None
    assert await backend.get(account_key(ApiType.DISPLAY, True, "sandbox-renamed")) is not None


@pytest.mark.asyncio
async def test_remove_account_two_step(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    """remove_account requires a confirmation token before deleting."""
    _ = allow_account_changes
    await _store_account(backend, alias="remove-alias")

    first = await remove_account("remove-alias")
    second_without_token = await remove_account("remove-alias")
    removed = await remove_account(
        "remove-alias",
        confirmation_token=str(first["confirmation_token"]),
    )

    assert first["pending_removal"] is True
    assert second_without_token["pending_removal"] is True
    assert removed["removed"] is True
    assert removed["alias"] == "remove-alias"
    assert await backend.get(account_key(ApiType.DISPLAY, False, "remove-alias")) is None


@pytest.mark.asyncio
async def test_remove_account_sandbox_uses_selected_namespace(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    _ = allow_account_changes
    await _store_account(backend, alias="shared-remove", raw_id="production-id")
    await _store_account(backend, alias="shared-remove", raw_id="sandbox-id", sandbox=True)

    first = await remove_account("shared-remove", sandbox=True)
    removed = await remove_account(
        "shared-remove",
        sandbox=True,
        confirmation_token=str(first["confirmation_token"]),
    )

    assert removed["removed"] is True
    assert await backend.get(account_key(ApiType.DISPLAY, False, "shared-remove")) is not None
    assert await backend.get(account_key(ApiType.DISPLAY, True, "shared-remove")) is None


@pytest.mark.asyncio
async def test_remove_account_confirmation_expires(
    backend: KeyringBackend,
    allow_account_changes: None,
) -> None:
    """remove_account rejects a confirmation token after its 60s TTL."""
    _ = allow_account_changes
    await _store_account(backend, alias="expire-alias")

    with freeze_time("2026-05-22 12:00:00", tz_offset=0) as frozen:
        first = await remove_account("expire-alias")
        _ = frozen.tick(timedelta(seconds=61))
        response = await remove_account(
            "expire-alias",
            confirmation_token=str(first["confirmation_token"]),
        )

    assert response == {"error": "confirmation_expired_or_missing"}


@pytest.mark.asyncio
async def test_duplicate_alias_rejected_with_suggestion(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """complete_account_login rejects duplicate aliases with a numbered suggestion."""
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY)
    await _store_account(backend, alias="nordic-display-abcd")
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    add_response = await add_account(ApiType.DISPLAY, alias="nordic-display-efgh")
    redirect = _redirect_url("synthetic-code", str(add_response["state"]))

    response = await complete_account_login(redirect, alias_override="nordic-display-abcd")

    assert response == {"error": "alias_taken", "suggested": "nordic-display-abcd-1"}


async def _store_app_credentials(
    backend: KeyringBackend,
    api_type: ApiType,
    *,
    sandbox: bool = False,
    client_id: str = "test-client-id",
    client_secret: str = "test-client-secret",
    redirect_uri: str = REDIRECT_URI,
) -> None:
    payload = {
        "api_type": api_type.value,
        "sandbox": sandbox,
        "client_id": client_id,
        "client_secret": client_secret,
        "created_at": NOW.isoformat(),
        "redirect_uri": redirect_uri,
    }
    await backend.set(app_creds_key(api_type, sandbox), json.dumps(payload))


async def _store_app_credentials_without_redirect_uri(
    backend: KeyringBackend,
    api_type: ApiType,
    *,
    sandbox: bool = False,
    client_id: str = "test-client-id",
    client_secret: str = "test-client-secret",
) -> None:
    payload = {
        "api_type": api_type.value,
        "sandbox": sandbox,
        "client_id": client_id,
        "client_secret": client_secret,
        "created_at": NOW.isoformat(),
    }
    await backend.set(app_creds_key(api_type, sandbox), json.dumps(payload))


async def _store_account(
    backend: KeyringBackend,
    *,
    alias: str,
    raw_id: str = "test-open-id",
    sandbox: bool = False,
) -> None:
    account = Account(
        alias=alias,
        api_type=ApiType.DISPLAY,
        sandbox=sandbox,
        tiktok_id=raw_id,
        display_name="Demo Account",
        avatar_url=None,
        scopes=["user.info.basic"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
    )
    tokens = AccountTokens(
        access_token=SecretStr(f"{alias}-access"),
        refresh_token=SecretStr(f"{alias}-refresh"),
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_expires_at=NOW + timedelta(days=30),
        last_rotated_at=NOW,
    )
    await atomic_account_update(
        backend,
        account.api_type,
        account.sandbox,
        account.alias,
        account,
        tokens,
    )


def _mock_token_exchange(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    *,
    assert_request: Callable[[httpx.Request], None] | None = None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if assert_request is not None:
            assert_request(request)
        return httpx.Response(httpx.codes.OK, json=payload, request=request)

    def build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(accounts_module, "_build_http_client", build_client)


def _redirect_url(code: str, state: str) -> str:
    return f"{REDIRECT_URI}?{urllib.parse.urlencode({'code': code, 'state': state})}"
