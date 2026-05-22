# pyright: reportImportCycles=false, reportMissingTypeStubs=false

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from tiktok_mcp.auth.fingerprint import client_key_fingerprint
from tiktok_mcp.auth.keychain import (
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import KeychainUnavailableError

VALID_API_VALUES = {api.value for api in ApiType}


class AccountResourceEntry(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str
    api_type: ApiType
    sandbox: bool
    has_valid_token: bool
    expires_at: datetime
    last_used_at: datetime | None = None

    @classmethod
    def from_account(
        cls,
        account: Account,
        tokens: AccountTokens,
        now: datetime,
    ) -> AccountResourceEntry:
        access_token = tokens.access_token.get_secret_value()
        return cls(
            alias=account.alias,
            api_type=account.api_type,
            sandbox=account.sandbox,
            has_valid_token=bool(access_token) and tokens.access_token_expires_at > now,
            expires_at=tokens.access_token_expires_at,
            last_used_at=account.last_used_at,
        )


class AppCredentialResourceEntry(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    api_type: ApiType
    sandbox: bool
    client_key_fingerprint: str
    secret_set: bool
    sandbox_secret_set: bool
    registered_redirect_uri: str | None = None

    @classmethod
    def from_credentials(
        cls,
        credentials: AppCredentials,
        *,
        sandbox_secret_set: bool,
        registered_redirect_uri: str | None,
    ) -> AppCredentialResourceEntry:
        return cls(
            api_type=credentials.api_type,
            sandbox=credentials.sandbox,
            client_key_fingerprint=client_key_fingerprint(
                credentials.client_id.get_secret_value()
            ),
            secret_set=bool(credentials.client_secret.get_secret_value()),
            sandbox_secret_set=sandbox_secret_set,
            registered_redirect_uri=registered_redirect_uri,
        )


@app.resource("tiktok-mcp://accounts/", mime_type="application/json")
async def read_accounts_resource() -> list[dict[str, object]]:
    backend = await get_backend()
    now = datetime.now(UTC)
    entries: list[AccountResourceEntry] = []
    for key in await backend.list_keys("tiktok-mcp::"):
        if not _is_account_key(key):
            continue
        raw_record = await backend.get(key)
        if raw_record is None:
            continue
        try:
            account, tokens = deserialize_account_record(raw_record)
        except KeychainUnavailableError:
            continue
        entries.append(AccountResourceEntry.from_account(account, tokens, now))
    return [entry.model_dump(mode="json") for entry in entries]


@app.resource("tiktok-mcp://app-credentials/", mime_type="application/json")
async def read_app_credentials_resource() -> list[dict[str, object]]:
    backend = await get_backend()
    records: list[tuple[AppCredentials, str | None]] = []
    for key in await backend.list_keys("tiktok-mcp::"):
        if not _is_app_credentials_key(key):
            continue
        raw_record = await backend.get(key)
        if raw_record is None:
            continue
        try:
            records.append(_deserialize_app_credentials_record(raw_record))
        except (KeychainUnavailableError, ValidationError):
            continue

    sandbox_secret_by_api_type = {
        credentials.api_type: bool(credentials.client_secret.get_secret_value())
        for credentials, _redirect_uri in records
        if credentials.sandbox
    }

    entries = [
        AppCredentialResourceEntry.from_credentials(
            credentials,
            sandbox_secret_set=sandbox_secret_by_api_type.get(credentials.api_type, False),
            registered_redirect_uri=redirect_uri,
        )
        for credentials, redirect_uri in records
    ]
    return [entry.model_dump(mode="json") for entry in entries]


def _deserialize_app_credentials_record(raw_record: str) -> tuple[AppCredentials, str | None]:
    try:
        payload = cast(object, json.loads(raw_record))
    except json.JSONDecodeError as exc:
        raise KeychainUnavailableError("Stored app credentials are not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise KeychainUnavailableError("Stored app credentials must be a JSON object.")

    payload_dict_source = cast(dict[str, object], payload)
    payload_dict: dict[str, object] = {
        str(key): value for key, value in payload_dict_source.items()
    }
    credentials_payload, redirect_uri = _split_app_credentials_payload(payload_dict)
    credentials = AppCredentials.model_validate(credentials_payload)
    return credentials, redirect_uri


def _split_app_credentials_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object], str | None]:
    nested_credentials = payload.get("credentials")
    if isinstance(nested_credentials, dict):
        nested_credentials_dict = cast(dict[str, object], nested_credentials)
        credentials_payload: dict[str, object] = {
            str(key): value for key, value in nested_credentials_dict.items()
        }
        redirect_uri = _optional_string_value(payload, "redirect_uri")
        if redirect_uri is None:
            redirect_uri = _optional_string_value(credentials_payload, "redirect_uri")
    else:
        credentials_payload = payload
        redirect_uri = _optional_string_value(payload, "redirect_uri")

    filtered_credentials: dict[str, object] = {
        key: credentials_payload[key]
        for key in {"api_type", "sandbox", "client_id", "client_secret", "created_at"}
        if key in credentials_payload
    }
    return filtered_credentials, redirect_uri


def _optional_string_value(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _is_account_key(key: str) -> bool:
    parts = key.split("::")
    return (
        len(parts) == 5
        and parts[0] == "tiktok-mcp"
        and parts[1] in VALID_API_VALUES
        and parts[2] in {"sandbox", "production"}
        and parts[3] == "account"
    )


def _is_app_credentials_key(key: str) -> bool:
    parts = key.split("::")
    return (
        len(parts) == 4
        and parts[0] == "tiktok-mcp"
        and parts[1] in VALID_API_VALUES
        and parts[2] in {"sandbox", "production"}
        and parts[3] == "app_creds"
    )


__all__ = [
    "AccountResourceEntry",
    "AppCredentialResourceEntry",
    "read_accounts_resource",
    "read_app_credentials_resource",
]
