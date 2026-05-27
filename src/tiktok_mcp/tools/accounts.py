from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import string
import urllib.parse
import webbrowser
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from mcp.types import ToolAnnotations
from pydantic import SecretStr, ValidationError

from tiktok_mcp.api.business.urls import (
    BUSINESS_ACCESS_TOKEN_PATH,
    BUSINESS_AUTH_PATH,
    BUSINESS_TT_USER_TOKEN_PATH,
    business_url,
)
from tiktok_mcp.auth import state
from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError, safe_raise_for_status
from tiktok_mcp.auth.keychain import (
    EncryptedFileBackend,
    KeychainBackend,
    KeyringBackend,
    account_key,
    app_creds_key,
    atomic_account_update,
    deserialize_account_record,
    get_backend,
    serialize_account_record,
)
from tiktok_mcp.auth.redactor import register_token, unregister_token
from tiktok_mcp.auth.url_parser import parse_redirect_url
from tiktok_mcp.decorators import mark_read_only, require_account_changes_enabled
from tiktok_mcp.server import app
from tiktok_mcp.types import (
    Account,
    AccountStatus,
    AccountSummary,
    ApiType,
    BusinessApiError,
    OAuthHostMismatchError,
    OAuthStateInvalidError,
    TikTokMCPError,
)
from tiktok_mcp.types.accounts import AccountTokens
from tiktok_mcp.types.app_credentials import AppCredentials

DEFAULT_OAUTH_EXPIRES_SECONDS = 600

DISPLAY_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
DISPLAY_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
DEFAULT_LOOPBACK_REDIRECT_URI = "http://localhost:8765/callback"
LOOPBACK_BIND_HOST = "127.0.0.1"
_LOOPBACK_TIMEOUT_SECONDS = 300.0
_LOOPBACK_SUCCESS_HTML = (
    "<!doctype html><html><head><title>Authentication complete</title></head>"
    "<body><h1>Authentication complete</h1>"
    "<p>You can close this tab and return to your terminal.</p></body></html>"
)
_LOOPBACK_ERROR_HTML = (
    "<!doctype html><html><head><title>Authentication failed</title></head>"
    "<body><h1>Authentication failed</h1>"
    "<p>Return to your terminal and start the login flow again.</p></body></html>"
)
INSTRUCTIONS = (
    "Open the URL in your browser, authenticate with a sandbox-allowlisted TikTok account, "
    "then copy the FULL redirect URL from your browser's address bar and call "
    "complete_account_login with it. If TikTok shows a redirect_uri or Incorrect parameters "
    "error, the redirect URI registered with set_app_credentials must match the app's "
    "developer console exactly."
)
LOOPBACK_INSTRUCTIONS = (
    "Open the URL in your browser, sign in to TikTok, and approve the requested scopes. "
    "The local 127.0.0.1 listener will capture the redirect and finish the login automatically. "
    "If TikTok shows a redirect_uri or Incorrect parameters error, the redirect URI registered "
    "with set_app_credentials must match the app's developer console exactly."
)
ACCOUNT_KEY_RE = re.compile(
    r"^tiktok-mcp::(?P<api>display|marketing|business_organic|content_posting)::"
    r"(?P<mode>sandbox|production)::account::(?P<alias>[a-z0-9-]{3,50})$"
)
ALIAS_RE = re.compile(r"^[a-z0-9-]{3,50}$")
DISPLAY_SCOPES = ("user.info.basic", "video.list")
POSTING_SCOPES = ("user.info.basic", "video.publish", "video.upload")
ORGANIC_ACCOUNT_SCOPES = (
    "user.info.basic",
    "video.list",
    "comment.list",
    "comment.list.manage",
)
PKCE_APIS = frozenset({ApiType.DISPLAY, ApiType.CONTENT_POSTING})
LOOPBACK_DEFAULT_APIS = frozenset({ApiType.BUSINESS_ORGANIC, ApiType.MARKETING})
_PKCE_VERIFIER_CHARS = string.ascii_letters + string.digits + "-._~"
_TIKTOK_PKCE_VERIFIER_LENGTH = 64
_PENDING_REMOVALS: dict[str, tuple[str, datetime]] = {}
_PENDING_LOCK = asyncio.Lock()
_MAX_PENDING_REMOVALS = 100
_REMOVAL_TTL_SECONDS = 60
_PENDING_LOOPBACK_LOGINS: dict[str, PendingLoopbackLogin] = {}
_PENDING_LOOPBACK_LOCK = asyncio.Lock()
_MAX_PENDING_LOOPBACK_LOGINS = 20
_MAX_LOOPBACK_POLL_WAIT_SECONDS = 25


@dataclass(frozen=True)
class LoadedAppCredentials:
    credentials: AppCredentials
    redirect_uri: str


@dataclass(frozen=True)
class LoopbackRedirect:
    scheme: str
    netloc: str
    path: str
    port: int


@dataclass(frozen=True)
class PendingLoopbackLogin:
    state: str
    auth_url: str
    suggested_alias: str
    server: asyncio.AbstractServer
    loopback_redirect: LoopbackRedirect
    task: asyncio.Task[dict[str, Any]]
    created_at: datetime
    expires_at: datetime


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def add_account(
    api_type: ApiType,
    alias: str | None = None,
    sandbox: bool = False,
    await_callback: bool = False,
) -> dict[str, Any]:
    backend = await get_backend()
    try:
        loaded_credentials = await _load_app_credentials(backend, api_type, sandbox=sandbox)
    except TikTokMCPError as exc:
        return exc.to_dict()
    if loaded_credentials is None:
        return _app_credentials_not_set(api_type)

    suggested_alias = alias or _generate_alias(api_type)
    if not _valid_alias(suggested_alias):
        return _invalid_alias_error(suggested_alias)

    pkce_verifier = _new_pkce_verifier() if api_type in PKCE_APIS else None
    oauth_state = await state.create_state(
        api_type,
        suggested_alias,
        sandbox=sandbox,
        pkce_verifier=pkce_verifier,
    )
    url = _build_authorization_url(loaded_credentials, oauth_state.state, pkce_verifier)
    loopback_redirect = _loopback_redirect(loaded_credentials.redirect_uri)
    if await_callback:
        if loopback_redirect is None:
            return {
                "error": "loopback_redirect_required",
                "message": (
                    "Loopback callback capture requires an http://localhost:<port> "
                    "or http://127.0.0.1:<port> redirect_uri."
                ),
            }
        try:
            return await _start_pending_loopback_login(
                auth_url=url,
                state_token=oauth_state.state,
                suggested_alias=suggested_alias,
                redirect_uri=loaded_credentials.redirect_uri,
            )
        except OSError as exc:
            return _manual_authorization_response(
                url,
                oauth_state.state,
                suggested_alias,
                warning="oauth_loopback_unavailable",
                message=f"Could not start OAuth loopback listener: {type(exc).__name__}.",
                context={"reason": type(exc).__name__},
            )

    return _manual_authorization_response(url, oauth_state.state, suggested_alias)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def add_account_with_loopback(
    api_type: ApiType,
    sandbox: bool = False,
    alias: str | None = None,
    scopes: list[str] | None = None,
    callback_port: int | None = None,
) -> dict[str, Any]:
    backend = await get_backend()
    try:
        loaded_credentials = await _load_app_credentials(backend, api_type, sandbox=sandbox)
    except TikTokMCPError as exc:
        return exc.to_dict()
    if loaded_credentials is None:
        return _app_credentials_not_set(api_type)

    suggested_alias = alias or _generate_alias(api_type)
    if not _valid_alias(suggested_alias):
        return _invalid_alias_error(suggested_alias)

    pkce_verifier = _new_pkce_verifier() if api_type in PKCE_APIS else None
    oauth_state = await state.create_state(
        api_type,
        suggested_alias,
        sandbox=sandbox,
        pkce_verifier=pkce_verifier,
    )
    manual_auth_url = _build_authorization_url(
        loaded_credentials,
        oauth_state.state,
        pkce_verifier,
        scopes=scopes,
    )
    pending_error = await _pending_loopback_start_error(
        loaded_credentials.redirect_uri,
        callback_port=callback_port,
    )
    if pending_error is not None:
        return pending_error
    try:
        listener = await _start_loopback_server(
            loaded_credentials.redirect_uri,
            oauth_state.state,
            callback_port=callback_port,
        )
    except OSError as exc:
        return _manual_authorization_response(
            manual_auth_url,
            oauth_state.state,
            suggested_alias,
            warning="oauth_loopback_unavailable",
            message=(
                "Could not start OAuth loopback listener; use this URL and paste "
                "the redirect URL into complete_account_login."
            ),
            context={"reason": type(exc).__name__},
        )
    except ValueError as exc:
        return {"error": "invalid_callback_port", "message": str(exc)}
    if listener is None:
        return {
            "error": "loopback_redirect_required",
            "message": (
                "Loopback callback capture requires an http://localhost:<port> "
                "or http://127.0.0.1:<port> redirect_uri."
            ),
        }

    server, loopback_redirect, callback_future = listener
    loopback_redirect_url = _loopback_redirect_url(loopback_redirect)
    loopback_credentials = LoadedAppCredentials(
        credentials=loaded_credentials.credentials,
        redirect_uri=loopback_redirect_url,
    )
    auth_url = _build_authorization_url(
        loopback_credentials,
        oauth_state.state,
        pkce_verifier,
        scopes=scopes,
    )

    return await _register_pending_loopback_login(
        auth_url=auth_url,
        state_token=oauth_state.state,
        suggested_alias=suggested_alias,
        server=server,
        loopback_redirect=loopback_redirect,
        callback_future=callback_future,
        redirect_uri_override=loopback_redirect_url,
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def complete_account_login(
    redirect_url: str,
    alias_override: str | None = None,
) -> dict[str, Any]:
    return await _complete_account_login(redirect_url, alias_override=alias_override)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def poll_loopback_login(state: str, wait_seconds: int = 0) -> dict[str, Any]:
    wait_seconds = max(0, min(wait_seconds, _MAX_LOOPBACK_POLL_WAIT_SECONDS))
    await _drop_expired_loopback_logins()
    async with _PENDING_LOOPBACK_LOCK:
        pending_login = _PENDING_LOOPBACK_LOGINS.get(state)
    if pending_login is None:
        return {
            "error": "oauth_loopback_not_found",
            "message": "No pending OAuth loopback login exists for this state.",
            "state": state,
        }

    if not pending_login.task.done() and wait_seconds > 0:
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending_login.task), timeout=wait_seconds)

    if not pending_login.task.done():
        return {
            "status": "pending",
            "state": state,
            "url": pending_login.auth_url,
            "auth_url": pending_login.auth_url,
            "suggested_alias": pending_login.suggested_alias,
            "expires_in": _seconds_until(pending_login.expires_at),
            "poll_with": "poll_loopback_login",
        }

    result = await pending_login.task
    async with _PENDING_LOOPBACK_LOCK:
        _ = _PENDING_LOOPBACK_LOGINS.pop(state, None)
    return result


async def _complete_account_login(
    redirect_url: str,
    alias_override: str | None = None,
    *,
    redirect_uri_override: str | None = None,
) -> dict[str, Any]:
    try:
        parsed_redirect = parse_redirect_url(redirect_url)
        oauth_state = await state.consume_state(parsed_redirect["state"])
        backend = await get_backend()
        loaded_credentials = await _load_app_credentials(
            backend,
            oauth_state.api_type,
            sandbox=oauth_state.sandbox,
        )
        if loaded_credentials is None:
            return _app_credentials_not_set(oauth_state.api_type)

        redirect_uri = redirect_uri_override or loaded_credentials.redirect_uri
        _validate_redirect_host(redirect_uri, parsed_redirect["host"])
        payload = await _exchange_code_for_tokens(
            loaded_credentials,
            parsed_redirect["code"],
            oauth_state.pkce_verifier,
            redirect_uri=redirect_uri,
        )
        access_value, refresh_value = _extract_token_values(payload)
        register_token(access_value, "access_token")
        if refresh_value is not None:
            register_token(refresh_value, "refresh_token")

        alias = alias_override or oauth_state.suggested_alias
        if not _valid_alias(alias):
            return _invalid_alias_error(alias)
        if await _alias_exists(backend, alias, sandbox=oauth_state.sandbox):
            return {
                "error": "alias_taken",
                "suggested": await _next_available_alias(
                    backend,
                    alias,
                    sandbox=oauth_state.sandbox,
                ),
            }

        account, tokens = _account_record_from_token_payload(
            loaded_credentials.credentials.api_type,
            oauth_state.sandbox,
            alias,
            payload,
            access_value,
            refresh_value,
        )
        await atomic_account_update(
            backend,
            account.api_type,
            account.sandbox,
            account.alias,
            account,
            tokens,
        )
        return _summary_dict(account)
    except OAuthStateInvalidError as exc:
        return exc.to_dict()
    except OAuthHostMismatchError as exc:
        return exc.to_dict()
    except TikTokMCPError as exc:
        return exc.to_dict()
    except SanitizedHttpxError as exc:
        return {"error": "token_exchange_failed", "message": str(exc)}
    except ValueError as exc:
        return {"error": "invalid_redirect_url", "message": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": "token_exchange_failed", "message": type(exc).__name__}


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def list_accounts() -> dict[str, Any]:
    backend = await get_backend()
    summaries: list[dict[str, Any]] = []
    for key in await _account_keys(backend):
        raw_record = await backend.get(key)
        if raw_record is None:
            continue
        account, _tokens = deserialize_account_record(raw_record)
        summaries.append(_summary_dict(account))
    return {"accounts": summaries, "count": len(summaries)}


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def rename_account(
    old_alias: str,
    new_alias: str,
    sandbox: bool = False,
) -> dict[str, Any]:
    if not _valid_alias(new_alias):
        return _invalid_alias_error(new_alias)

    backend = await get_backend()
    old_key = await _find_account_key_by_alias(backend, old_alias, sandbox=sandbox)
    if old_key is None:
        return {"error": "account_not_found"}
    if await _alias_exists(backend, new_alias, sandbox=sandbox):
        return {
            "error": "alias_taken",
            "suggested": await _next_available_alias(backend, new_alias, sandbox=sandbox),
        }

    if isinstance(backend, KeyringBackend | EncryptedFileBackend):
        async with backend.lock:
            raw_record = await backend.get_unlocked(old_key)
            if raw_record is None:
                return {"error": "account_not_found"}
            account, tokens = deserialize_account_record(raw_record)
            updated_account = account.model_copy(update={"alias": new_alias})
            new_key = account_key(updated_account.api_type, updated_account.sandbox, new_alias)
            await backend.set_unlocked(new_key, serialize_account_record(updated_account, tokens))
            await backend.delete_unlocked(old_key)
        return _summary_dict(updated_account)

    raw_record = await backend.get(old_key)
    if raw_record is None:
        return {"error": "account_not_found"}
    account, tokens = deserialize_account_record(raw_record)
    updated_account = account.model_copy(update={"alias": new_alias})
    new_key = account_key(updated_account.api_type, updated_account.sandbox, new_alias)
    await backend.set(new_key, serialize_account_record(updated_account, tokens))
    await backend.delete(old_key)
    return _summary_dict(updated_account)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def remove_account(
    alias: str,
    sandbox: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    backend = await get_backend()
    account_key_for_alias = await _find_account_key_by_alias(backend, alias, sandbox=sandbox)
    if account_key_for_alias is None:
        return {"error": "account_not_found"}

    now = datetime.now(UTC)
    pending_key = _pending_removal_key(alias, sandbox)
    async with _PENDING_LOCK:
        _drop_expired_pending(now)
        if confirmation_token is None:
            token, expires_at = _pending_or_new(pending_key, now)
            _PENDING_REMOVALS[pending_key] = (token, expires_at)
            _evict_pending_removals()
            return {
                "pending_removal": True,
                "confirmation_token": token,
                "expires_in": _REMOVAL_TTL_SECONDS,
                "message": (
                    "Call remove_account again with this confirmation_token within 60s "
                    "to confirm deletion."
                ),
            }

        pending_removal = _PENDING_REMOVALS.get(pending_key)
        if pending_removal is None or pending_removal[1] < now:
            _ = _PENDING_REMOVALS.pop(pending_key, None)
            return {"error": "confirmation_expired_or_missing"}
        if not secrets.compare_digest(pending_removal[0], confirmation_token):
            return {"error": "confirmation_token_mismatch"}

        raw_record = await backend.get(account_key_for_alias)
        if raw_record is None:
            _ = _PENDING_REMOVALS.pop(pending_key, None)
            return {"error": "account_not_found"}
        _account, tokens = deserialize_account_record(raw_record)
        unregister_token(tokens.access_token.get_secret_value())
        if tokens.refresh_token is not None:
            unregister_token(tokens.refresh_token.get_secret_value())
        await backend.delete(account_key_for_alias)
        _ = _PENDING_REMOVALS.pop(pending_key, None)
        return {"removed": True, "alias": alias, "removed_at": datetime.now(UTC).isoformat()}


def _build_authorization_url(
    loaded_credentials: LoadedAppCredentials,
    state_token: str,
    pkce_verifier: str | None,
    scopes: list[str] | None = None,
) -> str:
    credentials = loaded_credentials.credentials
    client_id = credentials.client_id.get_secret_value()
    if credentials.api_type in PKCE_APIS:
        selected_scopes = scopes or (
            DISPLAY_SCOPES if credentials.api_type is ApiType.DISPLAY else POSTING_SCOPES
        )
        params = {
            "client_key": client_id,
            "scope": ",".join(selected_scopes),
            "response_type": "code",
            "redirect_uri": loaded_credentials.redirect_uri,
            "state": state_token,
        }
        if pkce_verifier is not None:
            params["code_challenge"] = _build_tiktok_pkce_challenge(pkce_verifier)
            params["code_challenge_method"] = "S256"
        return f"{DISPLAY_AUTH_URL}?{urllib.parse.urlencode(params)}"

    if credentials.api_type is ApiType.BUSINESS_ORGANIC:
        organic_scopes = scopes or ORGANIC_ACCOUNT_SCOPES
        params = {
            "client_key": client_id,
            "scope": ",".join(organic_scopes),
            "response_type": "code",
            "redirect_uri": loaded_credentials.redirect_uri,
            "state": state_token,
        }
        return f"{DISPLAY_AUTH_URL}?{urllib.parse.urlencode(params)}"

    params = {
        "app_id": client_id,
        "state": state_token,
        "redirect_uri": loaded_credentials.redirect_uri,
    }
    auth_url = business_url(BUSINESS_AUTH_PATH, sandbox=credentials.sandbox)
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


async def _exchange_code_for_tokens(
    loaded_credentials: LoadedAppCredentials,
    code: str,
    pkce_verifier: str | None,
    *,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    credentials = loaded_credentials.credentials
    if credentials.api_type in PKCE_APIS:
        effective_redirect_uri = redirect_uri or loaded_credentials.redirect_uri
        body = {
            "client_key": credentials.client_id.get_secret_value(),
            "client_secret": credentials.client_secret.get_secret_value(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": effective_redirect_uri,
        }
        if pkce_verifier is not None:
            body["code_verifier"] = pkce_verifier
        async with _build_http_client() as client:
            response = await client.post(
                DISPLAY_TOKEN_URL,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        await safe_raise_for_status(response)
        payload = _json_object(response)
        _raise_for_oauth_error(payload)
        return payload

    if credentials.api_type is ApiType.BUSINESS_ORGANIC:
        effective_redirect_uri = redirect_uri or loaded_credentials.redirect_uri
        body = {
            "client_id": credentials.client_id.get_secret_value(),
            "client_secret": credentials.client_secret.get_secret_value(),
            "grant_type": "authorization_code",
            "auth_code": code,
            "redirect_uri": effective_redirect_uri,
        }
        token_url = business_url(BUSINESS_TT_USER_TOKEN_PATH, sandbox=credentials.sandbox)
        async with _build_http_client() as client:
            response = await client.post(token_url, json=body)
        await safe_raise_for_status(response)
        payload = _json_object(response)
        if "code" in payload and payload.get("code") != 0:
            raise BusinessApiError(
                code=_int_value(payload, "code"),
                message=_string_value(
                    payload,
                    "message",
                    default="TikTok account OAuth token exchange failed",
                ),
                request_id=_optional_string_value(payload, "request_id"),
                context={"endpoint": urllib.parse.urlparse(token_url).path},
            )
        data = payload.get("data")
        if isinstance(data, dict):
            return {str(key): value for key, value in data.items()}
        return payload

    body = {
        "app_id": credentials.client_id.get_secret_value(),
        "secret": credentials.client_secret.get_secret_value(),
        "auth_code": code,
    }
    token_url = business_url(BUSINESS_ACCESS_TOKEN_PATH, sandbox=credentials.sandbox)
    async with _build_http_client() as client:
        response = await client.post(token_url, json=body)
    await safe_raise_for_status(response)
    payload = _json_object(response)
    if "code" in payload and payload.get("code") != 0:
        raise BusinessApiError(
            code=_int_value(payload, "code"),
            message=_string_value(
                payload,
                "message",
                default="Business OAuth token exchange failed",
            ),
            request_id=_optional_string_value(payload, "request_id"),
            context={"endpoint": urllib.parse.urlparse(token_url).path},
        )
    data = payload.get("data")
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    return payload


def _build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


async def _start_pending_loopback_login(
    *,
    auth_url: str,
    state_token: str,
    suggested_alias: str,
    redirect_uri: str,
    callback_port: int | None = None,
    redirect_uri_override: str | None = None,
) -> dict[str, Any]:
    pending_error = await _pending_loopback_start_error(redirect_uri, callback_port=callback_port)
    if pending_error is not None:
        return pending_error

    listener = await _start_loopback_server(
        redirect_uri,
        state_token,
        callback_port=callback_port,
    )
    if listener is None:
        return {
            "error": "loopback_redirect_required",
            "message": (
                "Loopback callback capture requires an http://localhost:<port> "
                "or http://127.0.0.1:<port> redirect_uri."
            ),
        }
    server, loopback_redirect, callback_future = listener
    return await _register_pending_loopback_login(
        auth_url=auth_url,
        state_token=state_token,
        suggested_alias=suggested_alias,
        server=server,
        loopback_redirect=loopback_redirect,
        callback_future=callback_future,
        redirect_uri_override=redirect_uri_override,
    )


async def _pending_loopback_start_error(
    redirect_uri: str,
    *,
    callback_port: int | None = None,
) -> dict[str, Any] | None:
    await _drop_expired_loopback_logins()
    initial_redirect = _loopback_redirect(redirect_uri, port=callback_port)
    if initial_redirect is None:
        return {
            "error": "loopback_redirect_required",
            "message": (
                "Loopback callback capture requires an http://localhost:<port> "
                "or http://127.0.0.1:<port> redirect_uri."
            ),
        }

    async with _PENDING_LOOPBACK_LOCK:
        active_login = _active_pending_loopback_on_port(initial_redirect.port)
    if active_login is None:
        return None
    return {
        "error": "oauth_loopback_already_pending",
        "message": "Another OAuth loopback login is already waiting on this callback port.",
        "state": active_login.state,
        "poll_with": "poll_loopback_login",
    }


async def _register_pending_loopback_login(
    *,
    auth_url: str,
    state_token: str,
    suggested_alias: str,
    server: asyncio.AbstractServer,
    loopback_redirect: LoopbackRedirect,
    callback_future: asyncio.Future[str],
    redirect_uri_override: str | None = None,
) -> dict[str, Any]:
    task = asyncio.create_task(
        _complete_pending_loopback_login(
            server,
            callback_future,
            redirect_uri_override=redirect_uri_override,
        )
    )
    now = datetime.now(UTC)
    pending_login = PendingLoopbackLogin(
        state=state_token,
        auth_url=auth_url,
        suggested_alias=suggested_alias,
        server=server,
        loopback_redirect=loopback_redirect,
        task=task,
        created_at=now,
        expires_at=now + timedelta(seconds=_LOOPBACK_TIMEOUT_SECONDS),
    )
    async with _PENDING_LOOPBACK_LOCK:
        _PENDING_LOOPBACK_LOGINS[state_token] = pending_login
        _evict_pending_loopback_logins()
    _ = webbrowser.open(auth_url)
    return _pending_loopback_response(pending_login)


async def _complete_pending_loopback_login(
    server: asyncio.AbstractServer,
    callback_future: asyncio.Future[str],
    *,
    redirect_uri_override: str | None = None,
) -> dict[str, Any]:
    try:
        redirect_url = await _wait_for_loopback_callback(server, callback_future)
    except TikTokMCPError as exc:
        return exc.to_dict()
    except OSError as exc:
        return {
            "error": "oauth_loopback_unavailable",
            "message": "Could not start OAuth loopback listener.",
            "context": {"reason": type(exc).__name__},
        }
    return await _complete_account_login(
        redirect_url,
        redirect_uri_override=redirect_uri_override,
    )


def _pending_loopback_response(pending_login: PendingLoopbackLogin) -> dict[str, Any]:
    return {
        "status": "pending",
        "state": pending_login.state,
        "url": pending_login.auth_url,
        "auth_url": pending_login.auth_url,
        "suggested_alias": pending_login.suggested_alias,
        "expires_in": _seconds_until(pending_login.expires_at),
        "poll_with": "poll_loopback_login",
        "instructions": LOOPBACK_INSTRUCTIONS,
    }


def _active_pending_loopback_on_port(port: int) -> PendingLoopbackLogin | None:
    for pending_login in _PENDING_LOOPBACK_LOGINS.values():
        if pending_login.task.done():
            continue
        if pending_login.loopback_redirect.port == port:
            return pending_login
    return None


async def _drop_expired_loopback_logins() -> None:
    now = datetime.now(UTC)
    expired: list[PendingLoopbackLogin] = []
    async with _PENDING_LOOPBACK_LOCK:
        for state_token, pending_login in list(_PENDING_LOOPBACK_LOGINS.items()):
            if pending_login.expires_at > now and not pending_login.task.cancelled():
                continue
            expired.append(pending_login)
            _ = _PENDING_LOOPBACK_LOGINS.pop(state_token, None)
    for pending_login in expired:
        if not pending_login.task.done():
            pending_login.task.cancel()
        pending_login.server.close()
        await pending_login.server.wait_closed()


def _evict_pending_loopback_logins() -> None:
    while len(_PENDING_LOOPBACK_LOGINS) > _MAX_PENDING_LOOPBACK_LOGINS:
        oldest_state = min(
            _PENDING_LOOPBACK_LOGINS,
            key=lambda key: _PENDING_LOOPBACK_LOGINS[key].created_at,
        )
        pending_login = _PENDING_LOOPBACK_LOGINS.pop(oldest_state)
        if not pending_login.task.done():
            pending_login.task.cancel()
        pending_login.server.close()


def _seconds_until(expires_at: datetime) -> int:
    remaining = expires_at - datetime.now(UTC)
    return max(0, int(remaining.total_seconds()))


async def _complete_loopback_login(
    authorization_url: str,
    loopback_redirect: LoopbackRedirect,
    expected_state: str,
    *,
    alias_override: str | None = None,
    redirect_uri_override: str | None = None,
) -> dict[str, Any]:
    try:
        redirect_url = await _capture_loopback_callback(
            authorization_url,
            loopback_redirect,
            expected_state,
        )
    except TikTokMCPError as exc:
        return exc.to_dict()
    except OSError as exc:
        return {
            "error": "oauth_loopback_unavailable",
            "message": (
                f"Could not start OAuth loopback listener on "
                f"{LOOPBACK_BIND_HOST}:{loopback_redirect.port}."
            ),
            "context": {"reason": type(exc).__name__},
        }
    return await _complete_account_login(
        redirect_url,
        alias_override=alias_override,
        redirect_uri_override=redirect_uri_override,
    )


async def _capture_loopback_callback(
    authorization_url: str,
    loopback_redirect: LoopbackRedirect,
    expected_state: str,
) -> str:
    loop = asyncio.get_running_loop()
    callback_future: asyncio.Future[str] = loop.create_future()

    async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_loopback_request(
            reader,
            writer,
            loopback_redirect,
            callback_future,
            expected_state,
        )

    server = await asyncio.start_server(
        handle_request,
        host=LOOPBACK_BIND_HOST,
        port=loopback_redirect.port,
    )
    return await _await_loopback_callback(server, authorization_url, callback_future)


async def _start_loopback_server(
    redirect_uri: str,
    expected_state: str,
    *,
    callback_port: int | None = None,
) -> tuple[asyncio.AbstractServer, LoopbackRedirect, asyncio.Future[str]] | None:
    if callback_port is not None and not 0 <= callback_port <= 65535:
        raise ValueError("callback_port must be between 0 and 65535.")

    initial_redirect = _loopback_redirect(redirect_uri, port=callback_port)
    if initial_redirect is None:
        return None

    loop = asyncio.get_running_loop()
    callback_future: asyncio.Future[str] = loop.create_future()
    loopback_redirect: LoopbackRedirect | None = None
    server: asyncio.AbstractServer | None = None

    async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal server
        if server is not None:
            server.close()
        if loopback_redirect is None:
            raise TikTokMCPError(
                "oauth_loopback_unavailable",
                "OAuth loopback listener was not initialized.",
            )
        await _handle_loopback_request(
            reader,
            writer,
            loopback_redirect,
            callback_future,
            expected_state,
        )

    server = await asyncio.start_server(
        handle_request,
        host=LOOPBACK_BIND_HOST,
        port=initial_redirect.port,
    )
    sockets: list[Any] = list(server.sockets or [])
    if not sockets:
        server.close()
        await server.wait_closed()
        raise OSError("OAuth loopback listener did not expose a socket.")

    bound_port = cast(tuple[str, int], sockets[0].getsockname())[1]
    loopback_redirect = _loopback_redirect(redirect_uri, port=bound_port)
    if loopback_redirect is None:
        server.close()
        await server.wait_closed()
        return None

    return server, loopback_redirect, callback_future


async def _await_loopback_callback(
    server: asyncio.AbstractServer,
    authorization_url: str,
    callback_future: asyncio.Future[str],
) -> str:
    _ = webbrowser.open(authorization_url)
    return await _wait_for_loopback_callback(server, callback_future)


async def _wait_for_loopback_callback(
    server: asyncio.AbstractServer,
    callback_future: asyncio.Future[str],
) -> str:
    try:
        return await asyncio.wait_for(callback_future, timeout=_LOOPBACK_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise TikTokMCPError(
            "oauth_loopback_timeout",
            f"OAuth loopback callback timed out after {_LOOPBACK_TIMEOUT_SECONDS:g} seconds.",
            {"timeout_seconds": _LOOPBACK_TIMEOUT_SECONDS},
        ) from exc
    finally:
        server.close()
        await server.wait_closed()


async def _handle_loopback_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    loopback_redirect: LoopbackRedirect,
    callback_future: asyncio.Future[str],
    expected_state: str,
) -> None:
    status = "200 OK"
    body = _LOOPBACK_SUCCESS_HTML
    try:
        request_line = await reader.readline()
        await _drain_http_headers(reader)
        redirect_url = _redirect_url_from_loopback_request(
            request_line,
            loopback_redirect,
            expected_state,
        )
    except TikTokMCPError as exc:
        status = "400 Bad Request"
        body = _LOOPBACK_ERROR_HTML
        if not callback_future.done():
            callback_future.set_exception(exc)
    else:
        if not callback_future.done():
            callback_future.set_result(redirect_url)

    await _write_loopback_response(writer, status, body)


async def _drain_http_headers(reader: asyncio.StreamReader) -> None:
    while True:
        line = await reader.readline()
        if line in {b"", b"\r\n", b"\n"}:
            return


def _redirect_url_from_loopback_request(
    request_line: bytes,
    loopback_redirect: LoopbackRedirect,
    expected_state: str,
) -> str:
    try:
        method, target, _version = (
            request_line.decode("ascii", errors="replace").strip().split(maxsplit=2)
        )
    except ValueError as exc:
        raise TikTokMCPError(
            "oauth_loopback_invalid_request",
            "OAuth loopback callback was not a valid HTTP request.",
        ) from exc

    if method != "GET":
        raise TikTokMCPError(
            "oauth_loopback_invalid_request",
            "OAuth loopback callback must use GET.",
        )

    parsed_target = urllib.parse.urlparse(target)
    if parsed_target.path != loopback_redirect.path:
        raise TikTokMCPError(
            "oauth_loopback_invalid_request",
            "OAuth loopback callback path did not match the registered redirect_uri.",
            {"expected_path": loopback_redirect.path, "actual_path": parsed_target.path},
        )

    query_params = urllib.parse.parse_qs(parsed_target.query, keep_blank_values=True)
    returned_state = _first_non_empty(query_params.get("state"))
    returned_code = _first_non_empty(query_params.get("code") or query_params.get("auth_code"))
    if returned_state is None or not secrets.compare_digest(returned_state, expected_state):
        raise OAuthStateInvalidError("unknown")
    if returned_code is None:
        raise TikTokMCPError(
            "oauth_loopback_invalid_request",
            "OAuth loopback callback was missing an authorization code.",
        )

    return urllib.parse.urlunparse(
        (
            loopback_redirect.scheme,
            loopback_redirect.netloc,
            loopback_redirect.path,
            "",
            parsed_target.query,
            "",
        )
    )


async def _write_loopback_response(
    writer: asyncio.StreamWriter,
    status: str,
    body: str,
) -> None:
    body_bytes = body.encode("utf-8")
    header = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    writer.write(header.encode("ascii") + body_bytes)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def _loopback_redirect(redirect_uri: str, *, port: int | None = None) -> LoopbackRedirect | None:
    parsed_uri = urllib.parse.urlparse(redirect_uri)
    hostname = parsed_uri.hostname.lower() if parsed_uri.hostname is not None else None
    if parsed_uri.scheme != "http" or hostname not in {"localhost", "127.0.0.1"}:
        return None
    resolved_port = parsed_uri.port if port is None else port
    if resolved_port is None:
        return None
    return LoopbackRedirect(
        scheme=parsed_uri.scheme,
        netloc=f"{hostname}:{resolved_port}",
        path=parsed_uri.path or "/",
        port=resolved_port,
    )


def _loopback_redirect_url(loopback_redirect: LoopbackRedirect) -> str:
    return urllib.parse.urlunparse(
        (
            loopback_redirect.scheme,
            loopback_redirect.netloc,
            loopback_redirect.path,
            "",
            "",
            "",
        )
    )


def _first_non_empty(values: list[str] | None) -> str | None:
    for value in values or []:
        if value:
            return value
    return None


async def _load_app_credentials(
    backend: KeychainBackend,
    api_type: ApiType,
    *,
    sandbox: bool,
) -> LoadedAppCredentials | None:
    raw_credentials = await backend.get(app_creds_key(api_type, sandbox))
    if raw_credentials is None:
        return None

    try:
        payload = cast(object, json.loads(raw_credentials))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    credentials_payload, redirect_uri = _split_credentials_payload(
        {str(key): value for key, value in payload.items()}
    )
    if not redirect_uri:
        if api_type in LOOPBACK_DEFAULT_APIS:
            redirect_uri = DEFAULT_LOOPBACK_REDIRECT_URI
        else:
            raise TikTokMCPError(
                "redirect_uri_not_set",
                (
                    f"App credentials exist for api_type={api_type.value} but no redirect_uri "
                    "is registered. Re-run set_app_credentials with redirect_uri set to the "
                    "exact URL registered in TikTok's developer console."
                ),
                {"api_type": api_type.value, "sandbox": sandbox},
            )
    try:
        credentials = AppCredentials.model_validate(credentials_payload)
    except ValidationError:
        return None
    return LoadedAppCredentials(credentials=credentials, redirect_uri=redirect_uri)


def _split_credentials_payload(payload: dict[str, object]) -> tuple[dict[str, object], str | None]:
    nested_credentials = payload.get("credentials")
    if isinstance(nested_credentials, dict):
        credentials_payload = {str(key): value for key, value in nested_credentials.items()}
        redirect_uri = _optional_string_value(payload, "redirect_uri")
        if redirect_uri is None:
            redirect_uri = _optional_string_value(credentials_payload, "redirect_uri")
    else:
        credentials_payload = payload
        redirect_uri = _optional_string_value(payload, "redirect_uri")

    filtered_credentials = {
        key: credentials_payload[key]
        for key in {"api_type", "sandbox", "client_id", "client_secret", "created_at"}
        if key in credentials_payload
    }
    return filtered_credentials, redirect_uri


def _validate_redirect_host(registered_redirect_uri: str, pasted_host: str) -> None:
    expected_host = urllib.parse.urlparse(registered_redirect_uri).hostname
    if not expected_host:
        msg = "Registered redirect URI must be an absolute URL with a host."
        raise ValueError(msg)
    normalized_expected = expected_host.lower()
    normalized_actual = pasted_host.lower()
    if normalized_expected != normalized_actual:
        raise OAuthHostMismatchError(normalized_expected, normalized_actual)


def _account_record_from_token_payload(
    api_type: ApiType,
    sandbox: bool,
    alias: str,
    payload: dict[str, Any],
    access_value: str,
    refresh_value: str | None,
) -> tuple[Account, AccountTokens]:
    now = datetime.now(UTC)
    expires_in = _int_value(payload, "expires_in")
    refresh_expires_in = _first_optional_int_value(
        payload,
        "refresh_expires_in",
        "refresh_token_expires_in",
    )
    account = Account(
        alias=alias,
        api_type=api_type,
        sandbox=sandbox,
        tiktok_id=_tiktok_id_from_payload(api_type, payload),
        display_name=_optional_string_value(payload, "display_name"),
        avatar_url=_optional_string_value(payload, "avatar_url"),
        scopes=_scopes_from_payload(payload),
        created_at=now,
        last_used_at=None,
        status=AccountStatus.OK,
    )
    access_expires_at = now + timedelta(seconds=expires_in)
    refresh_expires_at = (
        now + timedelta(seconds=refresh_expires_in) if refresh_expires_in is not None else None
    )
    tokens = AccountTokens(
        access_token=SecretStr(access_value),
        refresh_token=SecretStr(refresh_value) if refresh_value is not None else None,
        access_token_expires_at=access_expires_at if "SecretStr" else access_expires_at,
        refresh_token_expires_at=refresh_expires_at if refresh_value is not None else None,
        last_rotated_at=now,
    )
    return account, tokens


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        msg = "Token exchange response was not valid JSON."
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "Token exchange response must be a JSON object."
        raise ValueError(msg)
    return {str(key): value for key, value in payload.items()}


def _raise_for_oauth_error(payload: Mapping[str, object]) -> None:
    tiktok_error = _optional_string_value(payload, "error")
    if tiktok_error is None:
        return
    error_description = _optional_string_value(payload, "error_description")
    message = error_description or f"TikTok OAuth token exchange failed: {tiktok_error}."
    context: dict[str, object] = {"tiktok_error": tiktok_error}
    log_id = _optional_string_value(payload, "log_id")
    if log_id is not None:
        context["log_id"] = log_id
    raise TikTokMCPError("token_exchange_failed", message, context)


def _extract_token_values(payload: dict[str, Any]) -> tuple[str, str | None]:
    access_value = _string_value(payload, _token_key("access"))
    refresh_value = _optional_string_value(payload, _token_key("refresh"))
    return access_value, refresh_value


def _token_key(prefix: str) -> str:
    return f"{prefix}_token"


def _tiktok_id_from_payload(api_type: ApiType, payload: dict[str, Any]) -> str:
    open_id = _optional_string_value(payload, "open_id")
    if open_id is not None:
        return open_id
    advertiser_ids = payload.get("advertiser_ids")
    if isinstance(advertiser_ids, list):
        for advertiser_id in advertiser_ids:
            if isinstance(advertiser_id, str) and advertiser_id:
                return advertiser_id
    advertiser_id = _optional_string_value(payload, "advertiser_id")
    if advertiser_id is not None:
        return advertiser_id
    return f"{api_type.value}-unknown"


def _scopes_from_payload(payload: dict[str, Any]) -> list[str]:
    scope_value = payload.get("scope")
    if isinstance(scope_value, str):
        return [scope for scope in scope_value.split(",") if scope]
    if isinstance(scope_value, list):
        return [scope for scope in scope_value if isinstance(scope, str) and scope]
    return []


def _int_value(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Token exchange response is missing integer field {key}."
        raise ValueError(msg)
    return value


def _optional_int_value(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Token exchange response field {key} must be an integer."
        raise ValueError(msg)
    return value


def _first_optional_int_value(payload: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        value = _optional_int_value(payload, key)
        if value is not None:
            return value
    return None


def _string_value(payload: Mapping[str, object], key: str, *, default: str | None = None) -> str:
    value = payload.get(key, default)
    if isinstance(value, str) and value:
        return value
    msg = f"Token exchange response is missing string field {key}."
    raise ValueError(msg)


def _optional_string_value(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


async def _account_keys(backend: KeychainBackend) -> list[str]:
    keys = await backend.list_keys("tiktok-mcp::")
    return [key for key in keys if ACCOUNT_KEY_RE.fullmatch(key)]


async def _find_account_key_by_alias(
    backend: KeychainBackend,
    alias: str,
    *,
    sandbox: bool | None = None,
) -> str | None:
    for key in await _account_keys(backend):
        match = ACCOUNT_KEY_RE.fullmatch(key)
        if match is None or match.group("alias") != alias:
            continue
        if sandbox is not None and match.group("mode") != _mode_for_sandbox(sandbox):
            continue
        return key
    return None


async def _alias_exists(
    backend: KeychainBackend,
    alias: str,
    *,
    sandbox: bool | None = None,
) -> bool:
    return await _find_account_key_by_alias(backend, alias, sandbox=sandbox) is not None


async def _next_available_alias(
    backend: KeychainBackend,
    alias: str,
    *,
    sandbox: bool | None = None,
) -> str:
    suffix = 1
    while await _alias_exists(backend, f"{alias}-{suffix}", sandbox=sandbox):
        suffix += 1
    return f"{alias}-{suffix}"


def _mode_for_sandbox(sandbox: bool) -> str:
    return "sandbox" if sandbox else "production"


def _summary_dict(account: Account) -> dict[str, Any]:
    return AccountSummary.from_account(account).model_dump(mode="json")


def _app_credentials_not_set(api_type: ApiType) -> dict[str, str]:
    return {
        "error": "app_credentials_not_set",
        "message": f"Run set_app_credentials for api_type={api_type.value} first.",
    }


def _manual_authorization_response(
    url: str,
    state_token: str,
    suggested_alias: str,
    *,
    warning: str | None = None,
    message: str | None = None,
    context: dict[str, object] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "url": url,
        "state": state_token,
        "suggested_alias": suggested_alias,
        "expires_in": DEFAULT_OAUTH_EXPIRES_SECONDS,
        "instructions": INSTRUCTIONS,
    }
    if warning is not None:
        response["warning"] = warning
    if message is not None:
        response["message"] = message
    if context is not None:
        response["context"] = context
    return response


def _invalid_alias_error(alias: str) -> dict[str, str]:
    return {
        "error": "invalid_alias",
        "message": "Alias must match ^[a-z0-9-]{3,50}$.",
        "alias": alias,
    }


def _generate_alias(api_type: ApiType) -> str:
    return f"nordic-{_api_short_name(api_type)}-{secrets.token_hex(3)}"


def _api_short_name(api_type: ApiType) -> str:
    return {
        ApiType.DISPLAY: "display",
        ApiType.MARKETING: "marketing",
        ApiType.BUSINESS_ORGANIC: "comments",
        ApiType.CONTENT_POSTING: "posting",
    }[api_type]


def _valid_alias(alias: str) -> bool:
    return ALIAS_RE.fullmatch(alias) is not None


def build_rfc7636_pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_tiktok_pkce_challenge(code_verifier: str) -> str:
    return hashlib.sha256(code_verifier.encode("ascii")).hexdigest()


def _new_pkce_verifier() -> str:
    verifier = "".join(
        secrets.choice(_PKCE_VERIFIER_CHARS) for _ in range(_TIKTOK_PKCE_VERIFIER_LENGTH)
    )
    if not 43 <= len(verifier) <= 128:
        raise ValueError(f"Generated PKCE verifier has invalid length {len(verifier)}.")
    return verifier


def _pending_or_new(pending_key: str, now: datetime) -> tuple[str, datetime]:
    pending = _PENDING_REMOVALS.get(pending_key)
    if pending is not None and pending[1] >= now:
        return pending
    return secrets.token_urlsafe(16), now + timedelta(seconds=_REMOVAL_TTL_SECONDS)


def _pending_removal_key(alias: str, sandbox: bool) -> str:
    return f"{_mode_for_sandbox(sandbox)}::{alias}"


def _drop_expired_pending(now: datetime) -> None:
    for alias, (_token, expires_at) in list(_PENDING_REMOVALS.items()):
        if expires_at < now:
            del _PENDING_REMOVALS[alias]


def _evict_pending_removals() -> None:
    while len(_PENDING_REMOVALS) > _MAX_PENDING_REMOVALS:
        del _PENDING_REMOVALS[next(iter(_PENDING_REMOVALS))]


__all__ = [
    "add_account",
    "add_account_with_loopback",
    "complete_account_login",
    "list_accounts",
    "poll_loopback_login",
    "remove_account",
    "rename_account",
]
