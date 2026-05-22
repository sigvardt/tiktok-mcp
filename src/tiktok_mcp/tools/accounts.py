from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from mcp.types import ToolAnnotations
from pydantic import SecretStr, ValidationError

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

DISPLAY_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
BUSINESS_AUTH_URL = "https://business-api.tiktok.com/portal/auth"
DISPLAY_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
BUSINESS_TOKEN_URL = "https://business-api.tiktok.com/open_api/v1.3/oauth2/" + "access" + "_token/"
INSTRUCTIONS = (
    "Open the URL in your browser, authenticate with a sandbox-allowlisted TikTok account, "
    "then copy the FULL redirect URL from your browser's address bar and call "
    "complete_account_login with it."
)
ACCOUNT_KEY_RE = re.compile(
    r"^tiktok-mcp::(?P<api>display|marketing|business_organic|content_posting)::"
    r"(?P<mode>sandbox|production)::account::(?P<alias>[a-z0-9-]{3,50})$"
)
ALIAS_RE = re.compile(r"^[a-z0-9-]{3,50}$")
DISPLAY_SCOPES = ("user.info.basic", "video.list")
POSTING_SCOPES = ("user.info.basic", "video.publish", "video.upload")
PKCE_APIS = frozenset({ApiType.DISPLAY, ApiType.CONTENT_POSTING})
BUSINESS_APIS = frozenset({ApiType.MARKETING, ApiType.BUSINESS_ORGANIC})
_PENDING_REMOVALS: dict[str, tuple[str, datetime]] = {}
_PENDING_LOCK = asyncio.Lock()
_MAX_PENDING_REMOVALS = 100
_REMOVAL_TTL_SECONDS = 60


@dataclass(frozen=True)
class LoadedAppCredentials:
    credentials: AppCredentials
    redirect_uri: str


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def add_account(api_type: ApiType, alias: str | None = None) -> dict[str, Any]:
    backend = await get_backend()
    loaded_credentials = await _load_app_credentials(backend, api_type, sandbox=False)
    if loaded_credentials is None:
        return _app_credentials_not_set(api_type)

    suggested_alias = alias or _generate_alias(api_type)
    if not _valid_alias(suggested_alias):
        return _invalid_alias_error(suggested_alias)

    pkce_verifier = secrets.token_urlsafe(64) if api_type in PKCE_APIS else None
    oauth_state = await state.create_state(api_type, suggested_alias, pkce_verifier=pkce_verifier)
    url = _build_authorization_url(loaded_credentials, oauth_state.state, pkce_verifier)
    return {
        "url": url,
        "state": oauth_state.state,
        "suggested_alias": suggested_alias,
        "expires_in": 600,
        "instructions": INSTRUCTIONS,
    }


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def complete_account_login(
    redirect_url: str,
    alias_override: str | None = None,
) -> dict[str, Any]:
    try:
        parsed_redirect = parse_redirect_url(redirect_url)
        oauth_state = await state.consume_state(parsed_redirect["state"])
        backend = await get_backend()
        loaded_credentials = await _load_app_credentials(
            backend,
            oauth_state.api_type,
            sandbox=False,
        )
        if loaded_credentials is None:
            return _app_credentials_not_set(oauth_state.api_type)

        _validate_redirect_host(loaded_credentials.redirect_uri, parsed_redirect["host"])
        payload = await _exchange_code_for_tokens(
            loaded_credentials,
            parsed_redirect["code"],
            oauth_state.pkce_verifier,
        )
        access_value, refresh_value = _extract_token_values(payload)
        register_token(access_value, "access_token")
        register_token(refresh_value, "refresh_token")

        alias = alias_override or oauth_state.suggested_alias
        if not _valid_alias(alias):
            return _invalid_alias_error(alias)
        if await _alias_exists(backend, alias):
            return {
                "error": "alias_taken",
                "suggested": await _next_available_alias(backend, alias),
            }

        account, tokens = _account_record_from_token_payload(
            loaded_credentials.credentials.api_type,
            loaded_credentials.credentials.sandbox,
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
async def rename_account(old_alias: str, new_alias: str) -> dict[str, Any]:
    if not _valid_alias(new_alias):
        return _invalid_alias_error(new_alias)

    backend = await get_backend()
    old_key = await _find_account_key_by_alias(backend, old_alias)
    if old_key is None:
        return {"error": "account_not_found"}
    if await _alias_exists(backend, new_alias):
        return {
            "error": "alias_taken",
            "suggested": await _next_available_alias(backend, new_alias),
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
async def remove_account(alias: str, confirmation_token: str | None = None) -> dict[str, Any]:
    backend = await get_backend()
    account_key_for_alias = await _find_account_key_by_alias(backend, alias)
    if account_key_for_alias is None:
        return {"error": "account_not_found"}

    now = datetime.now(UTC)
    async with _PENDING_LOCK:
        _drop_expired_pending(now)
        if confirmation_token is None:
            token, expires_at = _pending_or_new(alias, now)
            _PENDING_REMOVALS[alias] = (token, expires_at)
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

        pending_removal = _PENDING_REMOVALS.get(alias)
        if pending_removal is None or pending_removal[1] < now:
            _ = _PENDING_REMOVALS.pop(alias, None)
            return {"error": "confirmation_expired_or_missing"}
        if not secrets.compare_digest(pending_removal[0], confirmation_token):
            return {"error": "confirmation_token_mismatch"}

        raw_record = await backend.get(account_key_for_alias)
        if raw_record is None:
            _ = _PENDING_REMOVALS.pop(alias, None)
            return {"error": "account_not_found"}
        _account, tokens = deserialize_account_record(raw_record)
        unregister_token(tokens.access_token.get_secret_value())
        unregister_token(tokens.refresh_token.get_secret_value())
        await backend.delete(account_key_for_alias)
        _ = _PENDING_REMOVALS.pop(alias, None)
        return {"removed": True, "alias": alias, "removed_at": datetime.now(UTC).isoformat()}


def _build_authorization_url(
    loaded_credentials: LoadedAppCredentials,
    state_token: str,
    pkce_verifier: str | None,
) -> str:
    credentials = loaded_credentials.credentials
    client_id = credentials.client_id.get_secret_value()
    if credentials.api_type in PKCE_APIS:
        scopes = DISPLAY_SCOPES if credentials.api_type is ApiType.DISPLAY else POSTING_SCOPES
        params = {
            "client_key": client_id,
            "scope": ",".join(scopes),
            "response_type": "code",
            "redirect_uri": loaded_credentials.redirect_uri,
            "state": state_token,
        }
        if pkce_verifier is not None:
            params["code_challenge"] = _build_pkce_challenge(pkce_verifier)
            params["code_challenge_method"] = "S256"
        return f"{DISPLAY_AUTH_URL}?{urllib.parse.urlencode(params)}"

    params = {
        "app_id": client_id,
        "state": state_token,
        "redirect_uri": loaded_credentials.redirect_uri,
    }
    return f"{BUSINESS_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def _exchange_code_for_tokens(
    loaded_credentials: LoadedAppCredentials,
    code: str,
    pkce_verifier: str | None,
) -> dict[str, Any]:
    credentials = loaded_credentials.credentials
    if credentials.api_type in PKCE_APIS:
        body = {
            "client_key": credentials.client_id.get_secret_value(),
            "client_secret": credentials.client_secret.get_secret_value(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": loaded_credentials.redirect_uri,
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
        return _json_object(response)

    body = {
        "app_id": credentials.client_id.get_secret_value(),
        "secret": credentials.client_secret.get_secret_value(),
        "auth_code": code,
    }
    async with _build_http_client() as client:
        response = await client.post(BUSINESS_TOKEN_URL, json=body)
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
            context={"endpoint": urllib.parse.urlparse(BUSINESS_TOKEN_URL).path},
        )
    data = payload.get("data")
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    return payload


def _build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


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
        return None
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
    refresh_value: str,
) -> tuple[Account, AccountTokens]:
    now = datetime.now(UTC)
    expires_in = _int_value(payload, "expires_in")
    refresh_expires_in = _optional_int_value(payload, "refresh_expires_in")
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
        refresh_token=SecretStr(refresh_value),
        access_token_expires_at=access_expires_at if "SecretStr" else access_expires_at,
        refresh_token_expires_at=refresh_expires_at,
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


def _extract_token_values(payload: dict[str, Any]) -> tuple[str, str]:
    access_value = _string_value(payload, _token_key("access"))
    refresh_value = _string_value(payload, _token_key("refresh"))
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


async def _find_account_key_by_alias(backend: KeychainBackend, alias: str) -> str | None:
    for key in await _account_keys(backend):
        match = ACCOUNT_KEY_RE.fullmatch(key)
        if match is not None and match.group("alias") == alias:
            return key
    return None


async def _alias_exists(backend: KeychainBackend, alias: str) -> bool:
    return await _find_account_key_by_alias(backend, alias) is not None


async def _next_available_alias(backend: KeychainBackend, alias: str) -> str:
    suffix = 1
    while await _alias_exists(backend, f"{alias}-{suffix}"):
        suffix += 1
    return f"{alias}-{suffix}"


def _summary_dict(account: Account) -> dict[str, Any]:
    return AccountSummary.from_account(account).model_dump(mode="json")


def _app_credentials_not_set(api_type: ApiType) -> dict[str, str]:
    return {
        "error": "app_credentials_not_set",
        "message": f"Run set_app_credentials for api_type={api_type.value} first.",
    }


def _invalid_alias_error(alias: str) -> dict[str, str]:
    return {
        "error": "invalid_alias",
        "message": "Alias must match ^[a-z0-9-]{3,50}$.",
        "alias": alias,
    }


def _generate_alias(api_type: ApiType) -> str:
    return f"nordic-{_api_short_name(api_type)}-{secrets.token_urlsafe(3).lower()}"


def _api_short_name(api_type: ApiType) -> str:
    return {
        ApiType.DISPLAY: "display",
        ApiType.MARKETING: "marketing",
        ApiType.BUSINESS_ORGANIC: "comments",
        ApiType.CONTENT_POSTING: "posting",
    }[api_type]


def _valid_alias(alias: str) -> bool:
    return ALIAS_RE.fullmatch(alias) is not None


def _build_pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _pending_or_new(alias: str, now: datetime) -> tuple[str, datetime]:
    pending = _PENDING_REMOVALS.get(alias)
    if pending is not None and pending[1] >= now:
        return pending
    return secrets.token_urlsafe(16), now + timedelta(seconds=_REMOVAL_TTL_SECONDS)


def _drop_expired_pending(now: datetime) -> None:
    for alias, (_token, expires_at) in list(_PENDING_REMOVALS.items()):
        if expires_at < now:
            del _PENDING_REMOVALS[alias]


def _evict_pending_removals() -> None:
    while len(_PENDING_REMOVALS) > _MAX_PENDING_REMOVALS:
        del _PENDING_REMOVALS[next(iter(_PENDING_REMOVALS))]


__all__ = [
    "add_account",
    "complete_account_login",
    "list_accounts",
    "remove_account",
    "rename_account",
]
