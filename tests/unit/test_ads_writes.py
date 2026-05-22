from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false, reportExplicitAny=false
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import httpx
import pytest
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.server import app
from tiktok_mcp.tools import marketing_writes_ads as ads_tools
from tiktok_mcp.tools.marketing_writes_ads import (
    AD_CREATE_PATH,
    AD_DELETE_PATH,
    AD_STATUS_UPDATE_PATH,
    AD_UPDATE_PATH,
    create_ad,
    delete_ad,
    update_ad,
    update_ad_status,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "advertiser-123"
ADGROUP_ID = "adgroup-456"
AD_ID = "ad-789"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


class DecoratedWriteTool(Protocol):
    __tiktok_mcp_destructive__: bool
    __tiktok_mcp_write_api__: str


@pytest.mark.asyncio
async def test_blocked_create_ad_returns_writes_disabled_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)

    result = await create_ad(
        ALIAS,
        ADVERTISER_ID,
        ADGROUP_ID,
        "CUSTOM",
        "Launch single video",
        "SINGLE_VIDEO",
        "TT_USER",
        "identity-123",
        "Try it today",
        video_id="video-123",
    )

    assert result["error"] == "writes_disabled"
    assert result["api"] == "marketing"
    assert result["tool"] == "create_ad"
    assert build_calls == []


@pytest.mark.asyncio
async def test_spark_ads_requires_post_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    build_calls = _install_forbidden_business_client(monkeypatch)

    result = await create_ad(
        ALIAS,
        ADVERTISER_ID,
        ADGROUP_ID,
        "CUSTOM",
        "Spark ad without post",
        "SINGLE_VIDEO",
        "TT_USER",
        "identity-123",
        "Try it today",
        video_id="video-123",
        creative_authorized=True,
    )

    assert result["error"] == "validation_error"
    assert "spark_ads_post_id" in str(result["message"])
    assert build_calls == []


@pytest.mark.asyncio
async def test_ad_crud_posts_expected_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")

    def handler(request: httpx.Request) -> httpx.Response:
        response_by_path: dict[str, dict[str, object]] = {
            AD_CREATE_PATH: {"ad_id": AD_ID},
            AD_UPDATE_PATH: {"ad_id": AD_ID, "updated": True},
            AD_STATUS_UPDATE_PATH: {"ad_ids": [AD_ID], "operation_status": "DISABLE"},
            AD_DELETE_PATH: {"ad_ids": [AD_ID], "deleted": True},
        }
        return _business_response(request, response_by_path[request.url.path])

    requests = _install_business_client(monkeypatch, handler)

    created = await create_ad(
        ALIAS,
        ADVERTISER_ID,
        ADGROUP_ID,
        "CUSTOM",
        "Launch single video",
        "SINGLE_VIDEO",
        "TT_USER",
        "identity-123",
        "Try it today",
        video_id="video-123",
        landing_page_url="https://example.test/landing",
        call_to_action="LEARN_MORE",
        display_name="Demo Brand",
    )
    updated = await update_ad(
        ALIAS,
        ADVERTISER_ID,
        AD_ID,
        ad_name="Updated single video",
        ad_text="Updated copy",
    )
    status = await update_ad_status(ALIAS, ADVERTISER_ID, AD_ID, "DISABLE")
    deleted = await delete_ad(ALIAS, ADVERTISER_ID, AD_ID)

    assert created == {"ad_id": AD_ID}
    assert updated == {"ad_id": AD_ID, "updated": True}
    assert status == {"ad_ids": [AD_ID], "operation_status": "DISABLE"}
    assert deleted == {"ad_ids": [AD_ID], "deleted": True}
    assert [request.url.path for request in requests] == [
        AD_CREATE_PATH,
        AD_UPDATE_PATH,
        AD_STATUS_UPDATE_PATH,
        AD_DELETE_PATH,
    ]
    assert all(request.headers["Access-Token"] == "marketing-access-token" for request in requests)
    assert all("authorization" not in request.headers for request in requests)

    create_body = _json_body(requests[0])
    assert create_body["creative_material_mode"] == "CUSTOM"
    assert create_body["video_id"] == "video-123"
    assert create_body["adgroup_id"] == ADGROUP_ID
    assert create_body["landing_page_url"] == "https://example.test/landing"

    update_body = _json_body(requests[1])
    assert update_body == {
        "advertiser_id": ADVERTISER_ID,
        "ad_id": AD_ID,
        "ad_name": "Updated single video",
        "ad_text": "Updated copy",
        "creative_authorized": False,
    }
    assert _json_body(requests[2]) == {
        "advertiser_id": ADVERTISER_ID,
        "ad_ids": [AD_ID],
        "operation_status": "DISABLE",
    }
    assert _json_body(requests[3]) == {"advertiser_id": ADVERTISER_ID, "ad_ids": [AD_ID]}


def test_all_ad_write_tools_destructive_hint_registry() -> None:
    registry = cast(Any, app)._tool_manager
    for tool_name, tool_fn in {
        "create_ad": create_ad,
        "update_ad": update_ad,
        "update_ad_status": update_ad_status,
        "delete_ad": delete_ad,
    }.items():
        registered_tool = registry.get_tool(tool_name)
        marked_tool = cast(DecoratedWriteTool, tool_fn)
        assert registered_tool is not None
        assert registered_tool.annotations.destructiveHint is True
        assert marked_tool.__tiktok_mcp_destructive__ is True
        assert marked_tool.__tiktok_mcp_write_api__ == "marketing"


def _install_business_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def build_client(alias: str) -> BusinessAPIClient:
        assert alias == ALIAS
        return BusinessAPIClient(
            _account(),
            _credentials(),
            transport=httpx.MockTransport(recording_handler),
        )

    monkeypatch.setattr(ads_tools, "_build_business_client", build_client)
    return requests


def _install_forbidden_business_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    build_calls: list[str] = []

    async def build_client(alias: str) -> BusinessAPIClient:
        build_calls.append(alias)
        raise AssertionError("BusinessAPIClient must not be built before validation passes")

    monkeypatch.setattr(ads_tools, "_build_business_client", build_client)
    return build_calls


def _json_body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content.decode("utf-8")))


def _business_response(request: httpx.Request, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "message": "OK", "request_id": "req-ok", "data": data},
        request=request,
    )


def _account() -> AccountWithTokens:
    return AccountWithTokens(
        alias=ALIAS,
        api_type=ApiType.MARKETING,
        sandbox=True,
        tiktok_id=ADVERTISER_ID,
        display_name="Marketing Demo",
        avatar_url=None,
        scopes=["business.ad.write"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
        access_token=SecretStr("marketing-access-token"),
        refresh_token=SecretStr("marketing-refresh-token"),
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_expires_at=NOW + timedelta(days=30),
        last_rotated_at=NOW,
    )


def _credentials() -> AppCredentials:
    return AppCredentials(
        api_type=ApiType.MARKETING,
        sandbox=True,
        client_id=SecretStr("marketing-client-id"),
        client_secret=SecretStr("marketing-client-secret"),
        created_at=NOW,
    )
