from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportAny=false
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.auth.keychain import account_key, app_creds_key, serialize_account_record
from tiktok_mcp.tools import marketing_read as marketing_read_tools
from tiktok_mcp.tools.marketing_read import (
    ADVERTISER_INFO_PATH,
    USER_INFO_PATH,
    marketing_list_advertisers,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "adv-1"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


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


@pytest.mark.asyncio
async def test_list_advertisers_uses_correct_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend(tiktok_id=ADVERTISER_ID)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == ADVERTISER_INFO_PATH
        assert request.url.params["advertiser_ids"] == '["adv-1"]'
        return _business_response(
            request,
            {"list": [{"advertiser_id": ADVERTISER_ID, "advertiser_name": "Demo Advertiser"}]},
        )

    _install_client(monkeypatch, backend, handler)

    result = await marketing_list_advertisers(ALIAS)

    assert result == {
        "advertisers": [{"advertiser_id": ADVERTISER_ID, "advertiser_name": "Demo Advertiser"}]
    }
    assert requests[0].headers["Access-Token"] == "marketing-access"


@pytest.mark.asyncio
async def test_list_advertisers_returns_sentinel_without_oauth_advertiser_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = await _configured_backend(tiktok_id="marketing-unknown")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == USER_INFO_PATH
        return _business_response(
            request,
            {"core_user_id": "core-1", "display_name": "Sandbox User"},
        )

    _install_client(monkeypatch, backend, handler)

    result = await marketing_list_advertisers(ALIAS)

    assert result["endpoint_not_supported_for_this_token_type"] is True
    assert result["advertisers"] == []
    assert result["user"] == {"core_user_id": "core-1", "display_name": "Sandbox User"}


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    backend: MemoryBackend,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    async def fake_get_backend() -> MemoryBackend:
        return backend

    def build_business_client(
        account: Account,
        app_credentials: AppCredentials,
        tokens: AccountTokens,
        client_backend: MemoryBackend,
    ) -> BusinessAPIClient:
        return BusinessAPIClient(
            account,
            app_credentials,
            tokens=tokens,
            backend=client_backend,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(marketing_read_tools, "get_backend", fake_get_backend)
    monkeypatch.setattr(marketing_read_tools, "_build_business_client", build_business_client)


async def _configured_backend(*, tiktok_id: str) -> MemoryBackend:
    backend = MemoryBackend()
    await backend.set(
        app_creds_key(ApiType.MARKETING, True),
        json.dumps(
            {
                "api_type": ApiType.MARKETING.value,
                "sandbox": True,
                "client_id": "marketing-client-id",
                "client_secret": "marketing-client-secret",
                "created_at": NOW.isoformat(),
            }
        ),
    )
    account = Account(
        alias=ALIAS,
        api_type=ApiType.MARKETING,
        sandbox=True,
        tiktok_id=tiktok_id,
        display_name="Marketing Demo",
        avatar_url=None,
        scopes=["business.advertiser.read"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
    )
    tokens = AccountTokens(
        access_token=SecretStr("marketing-access"),
        refresh_token=SecretStr("marketing-refresh"),
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_expires_at=NOW + timedelta(days=30),
        last_rotated_at=NOW,
    )
    await backend.set(
        account_key(ApiType.MARKETING, True, ALIAS),
        serialize_account_record(account, tokens),
    )
    return backend


def _business_response(request: httpx.Request, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "message": "OK", "request_id": "req-ok", "data": data},
        request=request,
    )
