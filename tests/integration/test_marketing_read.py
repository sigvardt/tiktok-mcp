from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportAny=false
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.api.marketing import (
    Ad,
    AdGroup,
    Advertiser,
    AdvertiserInfo,
    BusinessCenter,
    Campaign,
)
from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.auth.keychain import (
    account_key,
    app_creds_key,
    deserialize_account_record,
    serialize_account_record,
)
from tiktok_mcp.observability.rate_limit_tracker import reset_tracker
from tiktok_mcp.tools import marketing_read as marketing_read_tools
from tiktok_mcp.tools.marketing_read import (
    AD_GET_PATH,
    ADGROUP_GET_PATH,
    ADVERTISER_INFO_PATH,
    BC_ADVERTISER_GET_PATH,
    BC_GET_PATH,
    CAMPAIGN_GET_PATH,
    marketing_get_ad,
    marketing_get_adgroup,
    marketing_get_advertiser_info,
    marketing_get_campaign,
    marketing_list_adgroups,
    marketing_list_ads,
    marketing_list_advertisers,
    marketing_list_bc_advertisers,
    marketing_list_business_centers,
    marketing_list_campaigns,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import AccountBrokenError, BusinessApiError, KeychainUnavailableError

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
ALIAS = "marketing-demo"
BC_ENDPOINTS_NOT_AVAILABLE_IN_SANDBOX_MESSAGE = (
    "Business Center endpoints are not available in the TikTok Business sandbox. "
    "Use a production account with TIKTOK_MCP_LIVE_ACCOUNT_SAFETY configured to enable."
)


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
def reset_marketing_rate_limits() -> Iterator[None]:
    reset_tracker()
    yield
    reset_tracker()


@pytest.fixture
def vcr_config(vcr_cassette_dir: str) -> dict[str, object]:
    return {
        "cassette_library_dir": vcr_cassette_dir,
        "filter_headers": [("Access-Token", "REDACTED")],
    }


@pytest.mark.asyncio
async def test_list_advertisers_returns_models_and_uses_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = await _configured_backend()
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.url.path == ADVERTISER_INFO_PATH
        assert request.url.params["advertiser_ids"] == '["marketing-demo-tiktok-id"]'
        return _business_response(
            request,
            {"list": [{"advertiser_id": "adv-1", "advertiser_name": "Demo Advertiser"}]},
        )

    _install_client(monkeypatch, backend, handler)

    result = await marketing_list_advertisers(ALIAS)

    assert result == {
        "advertisers": [{"advertiser_id": "adv-1", "advertiser_name": "Demo Advertiser"}]
    }
    assert seen_requests[0].headers["Access-Token"] == "marketing-access"
    assert "authorization" not in seen_requests[0].headers


@pytest.mark.asyncio
async def test_get_advertiser_info_passes_advertiser_ids_and_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = await _configured_backend()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ADVERTISER_INFO_PATH
        assert request.url.params["advertiser_ids"] == '["adv-1"]'
        assert request.url.params["fields"] == '["advertiser_id","currency"]'
        return _business_response(
            request,
            {"list": [{"advertiser_id": "adv-1", "currency": "USD"}]},
        )

    _install_client(monkeypatch, backend, handler)

    info = await marketing_get_advertiser_info(ALIAS, "adv-1", fields=["advertiser_id", "currency"])

    assert info == AdvertiserInfo(advertiser_id="adv-1", currency="USD")


@pytest.mark.asyncio
async def test_list_business_centers_returns_models(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == BC_GET_PATH
        return _business_response(request, {"list": [{"bc_id": "bc-1", "bc_name": "Demo BC"}]})

    _install_client(monkeypatch, backend, handler)

    business_centers = await marketing_list_business_centers(ALIAS)

    assert business_centers == [BusinessCenter(bc_id="bc-1", bc_name="Demo BC")]


@pytest.mark.asyncio
async def test_list_bc_advertisers_passes_asset_type(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == BC_ADVERTISER_GET_PATH
        assert request.url.params["bc_id"] == "bc-1"
        assert request.url.params["asset_type"] == "ADVERTISER"
        return _business_response(request, {"advertiser_list": [{"advertiser_id": "adv-2"}]})

    _install_client(monkeypatch, backend, handler)

    advertisers = await marketing_list_bc_advertisers(ALIAS, "bc-1")

    assert advertisers == [Advertiser(advertiser_id="adv-2")]


@pytest.mark.parametrize(
    ("tool_name", "endpoint", "alternative_tools"),
    [
        pytest.param(
            "marketing_list_business_centers",
            BC_GET_PATH,
            ["marketing_list_advertisers", "marketing_list_bc_advertisers"],
            id="business-centers",
        ),
        pytest.param(
            "marketing_list_bc_advertisers",
            BC_ADVERTISER_GET_PATH,
            ["marketing_list_advertisers"],
            id="bc-advertisers",
        ),
    ],
)
@pytest.mark.asyncio
async def test_bc_tools_return_sandbox_unavailable_envelope_on_sandbox_404(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    endpoint: str,
    alternative_tools: list[str],
) -> None:
    backend = await _configured_backend(sandbox=True)
    _install_request_error(monkeypatch, backend, status=404)

    result = await _call_bc_read_tool(tool_name)

    assert result == {
        "endpoint_not_available_in_sandbox": True,
        "tool": tool_name,
        "alias": ALIAS,
        "endpoint": endpoint,
        "message": BC_ENDPOINTS_NOT_AVAILABLE_IN_SANDBOX_MESSAGE,
        "alternative_tools": alternative_tools,
    }


@pytest.mark.parametrize(
    ("tool_name", "endpoint"),
    [
        pytest.param("marketing_list_business_centers", BC_GET_PATH, id="business-centers"),
        pytest.param("marketing_list_bc_advertisers", BC_ADVERTISER_GET_PATH, id="bc-advertisers"),
    ],
)
@pytest.mark.asyncio
async def test_bc_tools_raise_production_404(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    endpoint: str,
) -> None:
    backend = await _configured_backend(sandbox=False)
    _install_request_error(monkeypatch, backend, status=404)

    with pytest.raises(SanitizedHttpxError) as exc_info:
        _ = await _call_bc_read_tool(tool_name)

    assert exc_info.value.status == 404
    assert exc_info.value.url_path == endpoint


@pytest.mark.parametrize(
    ("tool_name", "endpoint"),
    [
        pytest.param("marketing_list_business_centers", BC_GET_PATH, id="business-centers"),
        pytest.param("marketing_list_bc_advertisers", BC_ADVERTISER_GET_PATH, id="bc-advertisers"),
    ],
)
@pytest.mark.asyncio
async def test_bc_tools_raise_sandbox_non_404_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    endpoint: str,
) -> None:
    backend = await _configured_backend(sandbox=True)
    _install_request_error(monkeypatch, backend, status=500)

    with pytest.raises(SanitizedHttpxError) as exc_info:
        _ = await _call_bc_read_tool(tool_name)

    assert exc_info.value.status == 500
    assert exc_info.value.url_path == endpoint


@pytest.mark.parametrize(
    "tool_name",
    [
        pytest.param("marketing_list_business_centers", id="business-centers"),
        pytest.param("marketing_list_bc_advertisers", id="bc-advertisers"),
    ],
)
@pytest.mark.asyncio
async def test_bc_tools_preserve_sandbox_success_path(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    backend = await _configured_backend(sandbox=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if tool_name == "marketing_list_business_centers":
            assert request.url.path == BC_GET_PATH
            return _business_response(request, {"list": [{"bc_id": "bc-1", "bc_name": "Demo BC"}]})
        assert request.url.path == BC_ADVERTISER_GET_PATH
        return _business_response(request, {"advertiser_list": [{"advertiser_id": "adv-2"}]})

    _install_client(monkeypatch, backend, handler)

    result = await _call_bc_read_tool(tool_name)

    if tool_name == "marketing_list_business_centers":
        assert result == [BusinessCenter(bc_id="bc-1", bc_name="Demo BC")]
    else:
        assert result == [Advertiser(advertiser_id="adv-2")]


@pytest.mark.asyncio
async def test_list_campaigns_preserves_native_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()
    upstream = {
        "list": [{"campaign_id": "camp-1", "campaign_name": "Awareness"}],
        "page": 2,
        "page_size": 25,
        "total_number": 51,
        "total_page": 3,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == CAMPAIGN_GET_PATH
        assert request.url.params["advertiser_id"] == "adv-1"
        assert request.url.params["filtering"] == '{"campaign_ids":["camp-1"]}'
        assert request.url.params["page"] == "2"
        assert request.url.params["page_size"] == "25"
        return _business_response(request, upstream)

    _install_client(monkeypatch, backend, handler)

    result = await marketing_list_campaigns(
        ALIAS,
        "adv-1",
        filtering={"campaign_ids": ["camp-1"]},
        page=2,
        page_size=25,
    )

    assert result == upstream


@pytest.mark.asyncio
async def test_list_adgroups_preserves_native_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()
    upstream = {
        "list": [{"adgroup_id": "ag-1", "adgroup_name": "Prospecting"}],
        "page": 1,
        "page_size": 50,
        "total_number": 1,
        "total_page": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ADGROUP_GET_PATH
        assert request.url.params["advertiser_id"] == "adv-1"
        assert "filtering" not in request.url.params
        return _business_response(request, upstream)

    _install_client(monkeypatch, backend, handler)

    result = await marketing_list_adgroups(ALIAS, "adv-1")

    assert result == upstream


@pytest.mark.asyncio
async def test_list_ads_preserves_native_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()
    upstream = {
        "list": [{"ad_id": "ad-1", "ad_name": "Creative"}],
        "page": 3,
        "page_size": 10,
        "total_number": 25,
        "total_page": 3,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == AD_GET_PATH
        assert request.url.params["page"] == "3"
        assert request.url.params["page_size"] == "10"
        return _business_response(request, upstream)

    _install_client(monkeypatch, backend, handler)

    result = await marketing_list_ads(ALIAS, "adv-1", page=3, page_size=10)

    assert result == upstream


@pytest.mark.asyncio
async def test_get_campaign_filters_by_campaign_id(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == CAMPAIGN_GET_PATH
        assert request.url.params["filtering"] == '{"campaign_ids":["camp-1"]}'
        assert request.url.params["fields"] == '["campaign_id","campaign_name"]'
        assert request.url.params["page_size"] == "1"
        return _business_response(
            request,
            {"list": [{"campaign_id": "camp-1", "campaign_name": "Awareness"}]},
        )

    _install_client(monkeypatch, backend, handler)

    campaign = await marketing_get_campaign(
        ALIAS,
        "adv-1",
        "camp-1",
        fields=["campaign_id", "campaign_name"],
    )

    assert campaign == Campaign(campaign_id="camp-1", campaign_name="Awareness")


@pytest.mark.asyncio
async def test_get_adgroup_filters_by_adgroup_id(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ADGROUP_GET_PATH
        assert request.url.params["filtering"] == '{"adgroup_ids":["ag-1"]}'
        assert request.url.params["fields"] == '["adgroup_id","campaign_id"]'
        return _business_response(
            request,
            {"list": [{"adgroup_id": "ag-1", "campaign_id": "camp-1"}]},
        )

    _install_client(monkeypatch, backend, handler)

    adgroup = await marketing_get_adgroup(
        ALIAS,
        "adv-1",
        "ag-1",
        fields=["adgroup_id", "campaign_id"],
    )

    assert adgroup == AdGroup(adgroup_id="ag-1", campaign_id="camp-1")


@pytest.mark.asyncio
async def test_get_ad_filters_by_ad_id(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = await _configured_backend()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == AD_GET_PATH
        assert request.url.params["filtering"] == '{"ad_ids":["ad-1"]}'
        assert request.url.params["fields"] == '["ad_id","ad_name"]'
        return _business_response(request, {"list": [{"ad_id": "ad-1", "ad_name": "Creative"}]})

    _install_client(monkeypatch, backend, handler)

    ad = await marketing_get_ad(ALIAS, "adv-1", "ad-1", fields=["ad_id", "ad_name"])

    assert ad == Ad(ad_id="ad-1", ad_name="Creative")


@pytest.mark.asyncio
async def test_business_envelope_errors_raise_business_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = await _configured_backend()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 40000, "message": "Invalid parameter", "request_id": "req-error"},
            request=request,
        )

    _install_client(monkeypatch, backend, handler)

    with pytest.raises(BusinessApiError) as exc_info:
        _ = await marketing_list_campaigns(ALIAS, "adv-1")

    assert exc_info.value.tiktok_code == 40000
    assert exc_info.value.request_id == "req-error"


@pytest.mark.asyncio
async def test_auth_error_without_refresh_token_does_not_persist_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = await _configured_backend(refresh_token=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 40105, "message": "Token expired", "request_id": "req-auth"},
            request=request,
        )

    _install_client(monkeypatch, backend, handler)

    with pytest.raises(AccountBrokenError) as exc_info:
        _ = await marketing_list_campaigns(ALIAS, "adv-1")

    assert exc_info.value.context["tiktok_code"] == 40105
    stored = backend.values[account_key(ApiType.MARKETING, False, ALIAS)]
    account, tokens = deserialize_account_record(stored)
    assert account.status is AccountStatus.OK
    assert tokens.refresh_token is None


@pytest.mark.asyncio
async def test_masked_app_credentials_raise_clear_error_and_preserve_account_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MemoryBackend()
    await _store_masked_app_credentials(backend)
    await _store_marketing_account(
        backend,
        alias=ALIAS,
        access_token="marketing-access",
        refresh_token=None,
    )

    async def fake_get_backend() -> MemoryBackend:
        return backend

    monkeypatch.setattr(marketing_read_tools, "get_backend", fake_get_backend)

    with pytest.raises(KeychainUnavailableError) as exc_info:
        _ = await marketing_list_campaigns(ALIAS, "adv-1")

    assert exc_info.value.context["error"] == "app_credentials_masked_or_invalid"
    stored = backend.values[account_key(ApiType.MARKETING, False, ALIAS)]
    account, tokens = deserialize_account_record(stored)
    assert account.status is AccountStatus.OK
    assert tokens.refresh_token is None


@pytest.mark.asyncio
async def test_multi_account_isolation_uses_distinct_access_token_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MemoryBackend()
    await _store_app_credentials(backend)
    await _store_marketing_account(backend, alias="marketing-one", access_token="token-one")
    await _store_marketing_account(backend, alias="marketing-two", access_token="token-two")
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["Access-Token"])
        return _business_response(
            request,
            {"list": [], "page": 1, "page_size": 50, "total_number": 0, "total_page": 0},
        )

    _install_client(monkeypatch, backend, handler)

    _ = await marketing_list_campaigns("marketing-one", "adv-1")
    _ = await marketing_list_campaigns("marketing-two", "adv-2")

    assert seen_tokens == ["token-one", "token-two"]


def test_all_marketing_tools_are_marked_read_only() -> None:
    tools = [
        marketing_get_ad,
        marketing_get_adgroup,
        marketing_get_advertiser_info,
        marketing_get_campaign,
        marketing_list_ads,
        marketing_list_adgroups,
        marketing_list_advertisers,
        marketing_list_bc_advertisers,
        marketing_list_business_centers,
        marketing_list_campaigns,
    ]

    assert all(getattr(tool, "__tiktok_mcp_read_only__", False) for tool in tools)


def test_marketing_vcr_config_scrubs_access_token(vcr_config: dict[str, object]) -> None:
    assert vcr_config["filter_headers"] == [("Access-Token", "REDACTED")]


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


def _install_request_error(
    monkeypatch: pytest.MonkeyPatch,
    backend: MemoryBackend,
    *,
    status: int,
) -> None:
    async def fake_get_backend() -> MemoryBackend:
        return backend

    async def fake_request(
        self: BusinessAPIClient,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        _ = self, method, kwargs
        raise SanitizedHttpxError(status=status, url_path=path, request_id="req-error")

    monkeypatch.setattr(marketing_read_tools, "get_backend", fake_get_backend)
    monkeypatch.setattr(BusinessAPIClient, "request", fake_request)


async def _configured_backend(
    *,
    sandbox: bool = False,
    refresh_token: str | None = "marketing-refresh",
) -> MemoryBackend:
    backend = MemoryBackend()
    await _store_app_credentials(backend, sandbox=sandbox)
    await _store_marketing_account(
        backend,
        alias=ALIAS,
        access_token="marketing-access",
        refresh_token=refresh_token,
        sandbox=sandbox,
    )
    return backend


async def _store_marketing_account(
    backend: MemoryBackend,
    *,
    alias: str,
    access_token: str,
    refresh_token: str | None = None,
    sandbox: bool = False,
) -> None:
    account = Account(
        alias=alias,
        api_type=ApiType.MARKETING,
        sandbox=sandbox,
        tiktok_id=f"{alias}-tiktok-id",
        display_name="Marketing Demo",
        avatar_url=None,
        scopes=["business.advertiser.read", "business.bc.read"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
    )
    tokens = AccountTokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token) if refresh_token is not None else None,
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_expires_at=NOW + timedelta(days=30) if refresh_token is not None else None,
        last_rotated_at=NOW,
    )
    await backend.set(
        account_key(ApiType.MARKETING, sandbox, alias),
        serialize_account_record(account, tokens),
    )


async def _store_app_credentials(backend: MemoryBackend, *, sandbox: bool = False) -> None:
    payload = {
        "api_type": ApiType.MARKETING.value,
        "sandbox": sandbox,
        "client_id": "marketing-client-id",
        "client_secret": "marketing-client-secret",
        "created_at": NOW.isoformat(),
    }
    await backend.set(app_creds_key(ApiType.MARKETING, sandbox), json.dumps(payload))


async def _store_masked_app_credentials(backend: MemoryBackend, *, sandbox: bool = False) -> None:
    payload = {
        "api_type": ApiType.MARKETING.value,
        "sandbox": sandbox,
        "client_id": "**********",
        "client_secret": "**********",
        "created_at": NOW.isoformat(),
    }
    await backend.set(app_creds_key(ApiType.MARKETING, sandbox), json.dumps(payload))


async def _call_bc_read_tool(tool_name: str) -> object:
    if tool_name == "marketing_list_business_centers":
        return await marketing_list_business_centers(ALIAS)
    if tool_name == "marketing_list_bc_advertisers":
        return await marketing_list_bc_advertisers(ALIAS, "bc-1")
    raise AssertionError(f"Unknown BC read tool: {tool_name}")


def _business_response(request: httpx.Request, data: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "message": "OK", "request_id": "req-ok", "data": data},
        request=request,
    )
