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
    remove_account,
    rename_account,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType

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
    yield
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
) -> list[str]:
    opened_urls: list[str] = []

    def open_browser(url: str) -> bool:
        opened_urls.append(url)

        async def hit_callback() -> None:
            parsed_url = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed_url.query)
            redirect_uri = urllib.parse.urlparse(params["redirect_uri"][0])
            callback_query = urllib.parse.urlencode(
                {"code": callback_code, "state": params["state"][0]}
            )
            callback_url = (
                f"http://127.0.0.1:{redirect_uri.port}{redirect_uri.path}?{callback_query}"
            )
            async with httpx.AsyncClient() as client:
                callback_response = await client.get(callback_url)
            assert callback_response.status_code == httpx.codes.OK
            assert "Authentication complete" in callback_response.text

        _ = asyncio.create_task(hit_callback())
        return True

    monkeypatch.setattr("tiktok_mcp.tools.accounts.webbrowser.open", open_browser)
    return opened_urls


@pytest.mark.asyncio
async def test_add_account_with_loopback_display_happy_path(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(backend, ApiType.DISPLAY, redirect_uri="http://localhost:8000/callback")
    _mock_token_exchange(monkeypatch, TOKEN_PAYLOAD)
    pkce_verifier = "A" * 43
    monkeypatch.setattr(accounts_module, "_new_pkce_verifier", lambda: pkce_verifier)
    opened_urls = _install_loopback_browser(monkeypatch)

    response = await add_account_with_loopback(
        ApiType.DISPLAY,
        alias="nordic-display-loopback",
        scopes=["user.info.basic", "video.list"],
    )

    parsed_auth_url = urllib.parse.urlparse(response["auth_url"])
    params = urllib.parse.parse_qs(parsed_auth_url.query)
    redirect_uri = urllib.parse.urlparse(params["redirect_uri"][0])
    expected_challenge = hashlib.sha256(pkce_verifier.encode("ascii")).hexdigest()

    assert opened_urls == [response["auth_url"]]
    assert response["alias"] == "nordic-display-loopback"
    assert response["api_type"] == "display"
    assert response["instructions"] == LOOPBACK_INSTRUCTIONS
    assert params["scope"] == ["user.info.basic,video.list"]
    assert params["code_challenge"] == [expected_challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert redirect_uri.hostname == "localhost"
    assert redirect_uri.port is not None
    assert await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-loopback"))


@pytest.mark.asyncio
async def test_add_account_with_loopback_business_happy_path(
    backend: KeyringBackend,
    allow_account_changes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = allow_account_changes
    await _store_app_credentials(
        backend,
        ApiType.MARKETING,
        sandbox=True,
        client_id="sandbox-client-id",
        redirect_uri="http://localhost:8000/callback",
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

    parsed_auth_url = urllib.parse.urlparse(response["auth_url"])
    params = urllib.parse.parse_qs(parsed_auth_url.query)

    assert opened_urls == [response["auth_url"]]
    assert response["alias"] == "nordic-marketing-loopback"
    assert response["api_type"] == "marketing"
    assert response["sandbox"] is True
    assert response["instructions"] == LOOPBACK_INSTRUCTIONS
    assert params["app_id"] == ["sandbox-client-id"]
    assert params["redirect_uri"][0].startswith("http://localhost:")
    assert "scope" not in params
    assert "code_challenge" not in params
    assert await backend.get(account_key(ApiType.MARKETING, True, "nordic-marketing-loopback"))


def _assert_business_oauth_request(request: httpx.Request) -> None:
    assert request.url.host == "sandbox-ads.tiktok.com"
    assert request.headers["Content-Type"].startswith("application/json")
    payload = json.loads(request.content.decode())
    assert payload == {
        "app_id": "sandbox-client-id",
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

    response = await add_account(ApiType.DISPLAY, alias="nordic-display-loopback")

    assert opened_urls
    assert response["alias"] == "nordic-display-loopback"
    assert response["api_type"] == "display"
    assert await backend.get(account_key(ApiType.DISPLAY, False, "nordic-display-loopback"))


@pytest.mark.asyncio
async def test_loopback_timeout(
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
    def open_browser_noop(url: str) -> bool:
        _ = url
        return True

    monkeypatch.setattr("tiktok_mcp.tools.accounts.webbrowser.open", open_browser_noop)

    response = await add_account(ApiType.DISPLAY, alias="nordic-display-timeout")

    assert response["error"] == "oauth_loopback_timeout"
    assert response["context"]["timeout_seconds"] == 0.01


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
    sandbox_record = await backend.get(
        account_key(ApiType.DISPLAY, True, "nordic-display-sandbox")
    )
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
