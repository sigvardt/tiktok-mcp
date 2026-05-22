# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from freezegun import freeze_time
from pydantic import SecretStr

import tiktok_mcp.auth.keychain as keychain_module
from tiktok_mcp.auth.keychain import account_key, app_creds_key, serialize_account_record
from tiktok_mcp.resources.accounts import (
    AccountResourceEntry,
    AppCredentialResourceEntry,
    read_accounts_resource,
    read_app_credentials_resource,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
FULL_CLIENT_KEY = "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555"
SANDBOX_CLIENT_KEY = "FFFF1111GGGG2222HHHH3333IIII4444JJJJ5555"


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
def reset_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryBackend]:
    backend = MemoryBackend()
    monkeypatch.setattr(keychain_module, "_backend", backend)
    yield backend
    monkeypatch.setattr(keychain_module, "_backend", None)


@pytest.mark.asyncio
async def test_accounts_resource_shape_and_no_secrets(reset_backend: MemoryBackend) -> None:
    await _store_account(
        reset_backend,
        alias="display-account",
        api_type=ApiType.DISPLAY,
        sandbox=False,
        access_token="display-access-token",
        expires_at=NOW + timedelta(hours=1),
        last_used_at=NOW - timedelta(minutes=5),
    )
    await _store_account(
        reset_backend,
        alias="marketing-account",
        api_type=ApiType.MARKETING,
        sandbox=True,
        access_token="marketing-access-token",
        expires_at=NOW - timedelta(minutes=1),
        last_used_at=None,
    )

    with freeze_time(NOW):
        response = cast(list[dict[str, object]], await read_accounts_resource())
    expected_keys = {
        "alias",
        "api_type",
        "sandbox",
        "has_valid_token",
        "expires_at",
        "last_used_at",
    }
    payload = json.dumps(response, sort_keys=True, ensure_ascii=False)
    entries = [AccountResourceEntry.model_validate(entry) for entry in response]
    entries_by_alias = {entry.alias: entry for entry in entries}

    assert len(response) == 2
    assert all(set(entry) == expected_keys for entry in response)
    assert entries_by_alias["display-account"].has_valid_token is True
    assert entries_by_alias["display-account"].expires_at == NOW + timedelta(hours=1)
    assert entries_by_alias["display-account"].last_used_at == NOW - timedelta(minutes=5)
    assert entries_by_alias["marketing-account"].has_valid_token is False
    assert "display-access-token" not in payload
    assert "marketing-access-token" not in payload
    assert "client_secret" not in payload
    assert re.search(r"[A-Z0-9]{40,}", payload, re.IGNORECASE) is None


@pytest.mark.asyncio
async def test_accounts_resource_empty_keychain_returns_empty_list() -> None:
    response = cast(list[dict[str, object]], await read_accounts_resource())

    assert response == []


@pytest.mark.asyncio
async def test_app_credentials_resource_shape_and_no_secrets(
    reset_backend: MemoryBackend,
) -> None:
    await _store_app_credentials(
        reset_backend,
        api_type=ApiType.DISPLAY,
        sandbox=False,
        client_id=FULL_CLIENT_KEY,
        client_secret="display-client-secret",
        redirect_uri="https://example.com/display/callback",
    )

    response = cast(list[dict[str, object]], await read_app_credentials_resource())
    expected_keys = {
        "api_type",
        "sandbox",
        "client_key_fingerprint",
        "secret_set",
        "sandbox_secret_set",
        "registered_redirect_uri",
    }
    payload = json.dumps(response, sort_keys=True, ensure_ascii=False)
    entries = [AppCredentialResourceEntry.model_validate(entry) for entry in response]

    assert len(response) == 1
    assert set(response[0]) == expected_keys
    assert entries[0].client_key_fingerprint == "AAAA…5555"
    assert entries[0].secret_set is True
    assert entries[0].sandbox_secret_set is False
    assert entries[0].registered_redirect_uri == "https://example.com/display/callback"
    assert FULL_CLIENT_KEY not in payload
    assert "display-client-secret" not in payload
    assert "client_secret" not in payload
    assert re.search(r"[A-Z0-9]{40,}", payload, re.IGNORECASE) is None


@pytest.mark.asyncio
async def test_app_credentials_resource_empty_keychain_returns_empty_list() -> None:
    response = cast(list[dict[str, object]], await read_app_credentials_resource())

    assert response == []


@pytest.mark.asyncio
async def test_app_credentials_resource_reports_sandbox_split(
    reset_backend: MemoryBackend,
) -> None:
    await _store_app_credentials(
        reset_backend,
        api_type=ApiType.DISPLAY,
        sandbox=False,
        client_id=FULL_CLIENT_KEY,
        client_secret="display-client-secret",
        redirect_uri="https://example.com/display/callback",
    )
    await _store_app_credentials(
        reset_backend,
        api_type=ApiType.DISPLAY,
        sandbox=True,
        client_id=SANDBOX_CLIENT_KEY,
        client_secret="sandbox-client-secret",
        redirect_uri="https://example.com/display/sandbox-callback",
    )

    response = cast(list[dict[str, object]], await read_app_credentials_resource())
    entries = [AppCredentialResourceEntry.model_validate(entry) for entry in response]
    sandbox_flags = {entry.sandbox for entry in entries}

    assert len(response) == 2
    assert sandbox_flags == {False, True}
    assert {entry.secret_set for entry in entries} == {True}
    assert {entry.sandbox_secret_set for entry in entries} == {True}


async def _store_account(
    backend: MemoryBackend,
    *,
    alias: str,
    api_type: ApiType,
    sandbox: bool,
    access_token: str,
    expires_at: datetime,
    last_used_at: datetime | None,
) -> None:
    account = Account(
        alias=alias,
        api_type=api_type,
        sandbox=sandbox,
        tiktok_id=f"{alias}-tiktok-id",
        display_name=alias.replace("-", " ").title(),
        avatar_url=None,
        scopes=["user.info.basic"],
        created_at=NOW,
        last_used_at=last_used_at,
        status=AccountStatus.OK,
    )
    tokens = AccountTokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(f"{alias}-refresh-token"),
        access_token_expires_at=expires_at,
        refresh_token_expires_at=NOW + timedelta(days=30),
        last_rotated_at=NOW,
    )
    await backend.set(
        account_key(api_type, sandbox, alias),
        serialize_account_record(account, tokens),
    )


async def _store_app_credentials(
    backend: MemoryBackend,
    *,
    api_type: ApiType,
    sandbox: bool,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> None:
    payload = {
        "api_type": api_type.value,
        "sandbox": sandbox,
        "client_id": client_id,
        "client_secret": client_secret,
        "created_at": NOW.isoformat(),
        "redirect_uri": redirect_uri,
    }
    await backend.set(app_creds_key(api_type, sandbox), json.dumps(payload, separators=(",", ":")))
