"""MCP tools for managing TikTok app credentials."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeAlias, cast

import httpx
from mcp.types import ToolAnnotations
from pydantic import SecretStr

from tiktok_mcp.api.business.urls import BUSINESS_ACCESS_TOKEN_PATH, business_oauth_url
from tiktok_mcp.auth.keychain import app_creds_key, get_backend
from tiktok_mcp.auth.redactor import register_token
from tiktok_mcp.decorators import mark_read_only, require_account_changes_enabled
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import ApiType
from tiktok_mcp.types.app_credentials import (
    AppCredentials,
    AppCredentialsSummary,
    AppCredentialsVerifyResult,
)
from tiktok_mcp.types.errors import KeychainUnavailableError

APP_CREDS_KEY_PREFIX = "tiktok-mcp::"
APP_CREDS_KEY_SUFFIX = "::app_creds"
DISPLAY_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
DISPLAY_LIKE_APIS = frozenset({ApiType.DISPLAY, ApiType.CONTENT_POSTING})
INVALID_AUTH_CODE_CODES = frozenset({40000, 40105})
CLIENT_SECRET_FIELD = "client_secret"
SecretStrInput: TypeAlias = str


@dataclass(frozen=True)
class StoredAppCredentials:
    credentials: AppCredentials
    redirect_uri: str | None


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_account_changes_enabled
async def set_app_credentials(
    api_type: ApiType,
    client_id: str,
    client_secret: SecretStrInput,
    sandbox: bool = False,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """Store TikTok app credentials and return a fingerprint-only summary."""
    api = _coerce_api_type(api_type)
    _validate_non_empty(client_id, "client_id")
    _validate_non_empty(SecretStr(client_secret).get_secret_value(), "client_secret")
    if redirect_uri is not None:
        _validate_redirect_uri(redirect_uri)

    credentials = AppCredentials(
        api_type=api,
        sandbox=sandbox,
        client_id=SecretStr(client_id),
        client_secret=SecretStr(client_secret),
        created_at=datetime.now(UTC),
    )
    await _write_app_credentials(credentials, redirect_uri=redirect_uri)
    return AppCredentialsSummary.from_credentials(
        credentials,
        registered_redirect_uri=redirect_uri,
    ).model_dump(mode="json")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def list_app_credentials() -> dict[str, Any]:
    """List stored TikTok app credentials as fingerprint-only summaries."""
    backend = await get_backend()
    keys = await backend.list_keys(APP_CREDS_KEY_PREFIX)
    credentials: list[dict[str, Any]] = []

    for key in sorted(key for key in keys if _is_app_creds_key(key)):
        stored = await backend.get(key)
        if stored is None:
            continue
        app_credentials = _deserialize_app_credentials_record(stored)
        credentials.append(
            AppCredentialsSummary.from_credentials(
                app_credentials.credentials,
                registered_redirect_uri=app_credentials.redirect_uri,
            ).model_dump(mode="json")
        )

    return {"credentials": credentials, "count": len(credentials)}


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def verify_app_credentials(api_type: ApiType, sandbox: bool = False) -> dict[str, Any]:
    """Verify stored app credentials with an ephemeral TikTok probe.

    Makes a single no-cost API probe to TikTok. Result is NOT persisted to keychain;
    caller should record it out-of-band if needed.

    Marketing and Business Organic use an intentionally empty auth-code probe. TikTok
    rejecting only that field means the app credentials were accepted well enough to parse.
    """
    api = _coerce_api_type(api_type)
    stored_credentials = await _read_app_credentials(api, sandbox)
    if stored_credentials is None:
        return AppCredentialsVerifyResult(
            api_type=api,
            sandbox=sandbox,
            client_id_fingerprint="",
            valid=False,
            registered_redirect_uri=None,
            error_code="not_found",
            error_message="No app credentials registered for this api_type/sandbox combo.",
        ).model_dump(mode="json")

    credentials = stored_credentials.credentials
    fingerprint = AppCredentialsSummary.from_credentials(credentials).client_id_fingerprint
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await _post_verification_probe(client, credentials)
    except httpx.HTTPError:
        return _verify_result(
            credentials,
            fingerprint,
            valid=False,
            registered_redirect_uri=stored_credentials.redirect_uri,
            error_code="network_error",
            error_message="Credential verification probe failed.",
        )

    if credentials.api_type in DISPLAY_LIKE_APIS:
        return _display_verify_result(
            credentials,
            fingerprint,
            response,
            registered_redirect_uri=stored_credentials.redirect_uri,
        )
    return _business_verify_result(
        credentials,
        fingerprint,
        response,
        registered_redirect_uri=stored_credentials.redirect_uri,
    )


async def _write_app_credentials(
    credentials: AppCredentials,
    *,
    redirect_uri: str | None = None,
) -> None:
    backend = await get_backend()
    await backend.set(
        app_creds_key(credentials.api_type, credentials.sandbox),
        _serialize_app_credentials(credentials, redirect_uri=redirect_uri),
    )


async def _read_app_credentials(api_type: ApiType, sandbox: bool) -> StoredAppCredentials | None:
    backend = await get_backend()
    stored = await backend.get(app_creds_key(api_type, sandbox))
    if stored is None:
        return None
    return _deserialize_app_credentials_record(stored)


def _serialize_app_credentials(
    credentials: AppCredentials,
    *,
    redirect_uri: str | None = None,
) -> str:
    _register_app_credentials(credentials)
    payload = credentials.model_dump(mode="json")
    payload["client_id"] = credentials.client_id.get_secret_value()
    payload[CLIENT_SECRET_FIELD] = _secret_value(credentials)
    if redirect_uri is not None:
        payload["redirect_uri"] = redirect_uri
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _deserialize_app_credentials(blob: str) -> AppCredentials:
    return _deserialize_app_credentials_record(blob).credentials


def _deserialize_app_credentials_record(blob: str) -> StoredAppCredentials:
    try:
        payload = cast(object, json.loads(blob))
    except json.JSONDecodeError as exc:
        raise KeychainUnavailableError("Stored app credentials are not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise KeychainUnavailableError("Stored app credentials are not a JSON object.")
    credentials_payload, redirect_uri = _split_credentials_payload(
        {str(key): value for key, value in payload.items()}
    )
    credentials = AppCredentials.model_validate(credentials_payload)
    _register_app_credentials(credentials)
    return StoredAppCredentials(credentials=credentials, redirect_uri=redirect_uri)


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
        for key in {"api_type", "sandbox", "client_id", CLIENT_SECRET_FIELD, "created_at"}
        if key in credentials_payload
    }
    return filtered_credentials, redirect_uri


def _register_app_credentials(credentials: AppCredentials) -> None:
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")


def _secret_value(credentials: AppCredentials) -> str:
    return SecretStr(credentials.client_secret.get_secret_value()).get_secret_value()


async def _post_verification_probe(
    client: httpx.AsyncClient,
    credentials: AppCredentials,
) -> httpx.Response:
    if credentials.api_type in DISPLAY_LIKE_APIS:
        return await client.post(
            DISPLAY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_key": credentials.client_id.get_secret_value(),
                CLIENT_SECRET_FIELD: _secret_value(credentials),
            },
        )

    return await client.post(
        business_oauth_url(BUSINESS_ACCESS_TOKEN_PATH),
        data={
            "app_id": credentials.client_id.get_secret_value(),
            "secret": _secret_value(credentials),
            "auth_code": "",
        },
    )


def _display_verify_result(
    credentials: AppCredentials,
    fingerprint: str,
    response: httpx.Response,
    *,
    registered_redirect_uri: str | None,
) -> dict[str, Any]:
    payload = _response_payload(response)
    credential_error = _credential_error_code(payload)
    if credential_error is not None:
        return _verify_result(
            credentials,
            fingerprint,
            valid=False,
            registered_redirect_uri=registered_redirect_uri,
            error_code=credential_error,
            error_message="TikTok rejected the app credentials.",
        )
    if response.status_code == httpx.codes.OK:
        return _verify_result(
            credentials,
            fingerprint,
            valid=True,
            registered_redirect_uri=registered_redirect_uri,
        )
    return _network_error_verify_result(
        credentials,
        fingerprint,
        response.status_code,
        registered_redirect_uri=registered_redirect_uri,
    )


def _business_verify_result(
    credentials: AppCredentials,
    fingerprint: str,
    response: httpx.Response,
    *,
    registered_redirect_uri: str | None,
) -> dict[str, Any]:
    payload = _response_payload(response)
    credential_error = _credential_error_code(payload)
    if credential_error is not None:
        return _verify_result(
            credentials,
            fingerprint,
            valid=False,
            registered_redirect_uri=registered_redirect_uri,
            error_code=credential_error,
            error_message="TikTok rejected the app credentials.",
        )
    if response.status_code == httpx.codes.OK or _has_invalid_auth_code(payload):
        return _verify_result(
            credentials,
            fingerprint,
            valid=True,
            registered_redirect_uri=registered_redirect_uri,
        )
    return _network_error_verify_result(
        credentials,
        fingerprint,
        response.status_code,
        registered_redirect_uri=registered_redirect_uri,
    )


def _verify_result(
    credentials: AppCredentials,
    fingerprint: str,
    *,
    valid: bool,
    registered_redirect_uri: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    return AppCredentialsVerifyResult(
        api_type=credentials.api_type,
        sandbox=credentials.sandbox,
        client_id_fingerprint=fingerprint,
        valid=valid,
        registered_redirect_uri=registered_redirect_uri,
        verified_at=datetime.now(UTC),
        error_code=error_code,
        error_message=error_message,
    ).model_dump(mode="json")


def _network_error_verify_result(
    credentials: AppCredentials,
    fingerprint: str,
    status_code: int,
    *,
    registered_redirect_uri: str | None = None,
) -> dict[str, Any]:
    return _verify_result(
        credentials,
        fingerprint,
        valid=False,
        registered_redirect_uri=registered_redirect_uri,
        error_code="network_error",
        error_message=f"Credential verification probe failed with HTTP {status_code}.",
    )


def _response_payload(response: httpx.Response) -> dict[str, object]:
    try:
        decoded = cast(object, response.json())
    except ValueError:
        return {}
    if isinstance(decoded, dict):
        return cast(dict[str, object], decoded)
    return {}


def _credential_error_code(payload: dict[str, object]) -> str | None:
    if _payload_mentions(payload, "invalid_secret") or _payload_mentions(payload, "invalid secret"):
        return "invalid_secret"
    if _payload_mentions(payload, "invalid_client") or _payload_mentions(payload, "invalid client"):
        return "invalid_client"
    return None


def _has_invalid_auth_code(payload: dict[str, object]) -> bool:
    code = payload.get("code")
    if isinstance(code, int) and code in INVALID_AUTH_CODE_CODES:
        return True
    if isinstance(code, str) and code.isdecimal() and int(code) in INVALID_AUTH_CODE_CODES:
        return True
    return _payload_mentions(payload, "invalid auth_code") or _payload_mentions(
        payload,
        "invalid_auth_code",
    )


def _payload_mentions(value: object, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_payload_mentions(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_payload_mentions(item, needle) for item in value)
    return needle in str(value).lower()


def _is_app_creds_key(key: str) -> bool:
    parts = key.split("::")
    valid_api_values = {api.value for api in ApiType}
    return (
        len(parts) == 4
        and parts[0] == "tiktok-mcp"
        and parts[1] in valid_api_values
        and parts[2] in {"sandbox", "production"}
        and parts[3] == "app_creds"
    )


def _coerce_api_type(api_type: ApiType | str) -> ApiType:
    if isinstance(api_type, ApiType):
        return api_type
    try:
        return ApiType(api_type)
    except ValueError as exc:
        allowed_values = ", ".join(api.value for api in ApiType)
        raise ValueError(f"api_type must be one of: {allowed_values}") from exc


def _validate_non_empty(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _validate_redirect_uri(redirect_uri: str) -> None:
    parsed_uri = urllib.parse.urlparse(redirect_uri)
    if parsed_uri.scheme not in {"http", "https"} or not parsed_uri.hostname:
        raise ValueError("redirect_uri must be an absolute http(s) URL with a host")


def _optional_string_value(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


__all__ = ["list_app_credentials", "set_app_credentials", "verify_app_credentials"]
