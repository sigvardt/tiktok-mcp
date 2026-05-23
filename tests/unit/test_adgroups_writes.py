from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false, reportAttributeAccessIssue=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportUnknownMemberType=false, reportAny=false
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.server import app
from tiktok_mcp.tools import marketing_writes_adgroups as adgroup_tools
from tiktok_mcp.tools.marketing_writes_adgroups import (
    ADGROUP_CREATE_PATH,
    ADGROUP_DELETE_PATH,
    ADGROUP_STATUS_UPDATE_PATH,
    ADGROUP_UPDATE_PATH,
    CreateAdGroupRequest,
    JsonObject,
    create_adgroup,
    delete_adgroup,
    update_adgroup,
    update_adgroup_status,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "advertiser-123"
CAMPAIGN_ID = "campaign-456"
ADGROUP_ID = "adgroup-789"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
BLOCKED_WRITE_VALUES: tuple[str | None, ...] = (None, "", "0", "false", "False", "no", "comments")


@pytest.mark.asyncio
@pytest.mark.parametrize("env_value", BLOCKED_WRITE_VALUES)
async def test_blocked(env_value: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_writes_env(monkeypatch, env_value)

    create_result = await create_adgroup(
        ALIAS,
        ADVERTISER_ID,
        CAMPAIGN_ID,
        "Nordic Prospecting",
        "PLACEMENT_TYPE_AUTOMATIC",
        "SCHEDULE_START_END",
        "2026-05-23 00:00:00",
        "CPC",
        "CLICK",
        "BID_TYPE_CUSTOM",
        "BUDGET_MODE_DAY",
        100.0,
        _targeting(["3144096"]),
        promotion_type="WEBSITE",
        schedule_end_time="2026-05-30 00:00:00",
    )
    update_result = await update_adgroup(ALIAS, ADVERTISER_ID, ADGROUP_ID, budget=150.0)
    status_result = await update_adgroup_status(
        ALIAS,
        ADVERTISER_ID,
        [ADGROUP_ID],
        "DISABLE",
    )
    delete_result = await delete_adgroup(ALIAS, ADVERTISER_ID, [ADGROUP_ID])

    for tool_name, result in (
        ("create_adgroup", create_result),
        ("update_adgroup", update_result),
        ("update_adgroup_status", status_result),
        ("delete_adgroup", delete_result),
    ):
        assert result["error"] == "writes_disabled"
        assert result["api"] == "marketing"
        assert result["tool"] == tool_name


@pytest.mark.asyncio
async def test_create_adgroup_posts_required_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == ADGROUP_CREATE_PATH
        assert request.headers["Access-Token"] == "adgroup-access-token"
        assert "authorization" not in request.headers
        body = _json_body(request)
        assert body == {
            "advertiser_id": ADVERTISER_ID,
            "campaign_id": CAMPAIGN_ID,
            "adgroup_name": "Nordic Prospecting",
            "placement_type": "PLACEMENT_TYPE_AUTOMATIC",
            "schedule_type": "SCHEDULE_START_END",
            "schedule_start_time": "2026-05-23 00:00:00",
            "schedule_end_time": "2026-05-30 00:00:00",
            "billing_event": "CPC",
            "optimization_goal": "CLICK",
            "bid_type": "BID_TYPE_CUSTOM",
            "budget_mode": "BUDGET_MODE_DAY",
            "budget": 100.0,
            "promotion_type": "WEBSITE",
            "location_ids": ["3144096", "2661886"],
            "genders": ["GENDER_FEMALE"],
            "languages": ["nb"],
            "bid_price": 2.5,
            "audience_ids": ["audience-1"],
        }
        return _business_response(
            request,
            {"adgroup_id": ADGROUP_ID, "status": "ENABLE", "modify_time": "2026-05-22T12:00:00Z"},
        )

    requests = _install_business_client(monkeypatch, handler)

    result = await create_adgroup(
        ALIAS,
        ADVERTISER_ID,
        CAMPAIGN_ID,
        "Nordic Prospecting",
        "PLACEMENT_TYPE_AUTOMATIC",
        "SCHEDULE_START_END",
        "2026-05-23 00:00:00",
        "CPC",
        "CLICK",
        "BID_TYPE_CUSTOM",
        "BUDGET_MODE_DAY",
        100.0,
        {
            "location_ids": ["3144096", "2661886"],
            "genders": ["GENDER_FEMALE"],
            "languages": ["nb"],
        },
        promotion_type="WEBSITE",
        schedule_end_time="2026-05-30 00:00:00",
        bid_price=2.5,
        audience_ids=["audience-1"],
    )

    assert len(requests) == 1
    assert result["adgroup_id"] == ADGROUP_ID
    assert result["status"] == "ENABLE"


@pytest.mark.asyncio
async def test_update_adgroup_posts_partial_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ADGROUP_UPDATE_PATH
        assert _json_body(request) == {
            "advertiser_id": ADVERTISER_ID,
            "adgroup_id": ADGROUP_ID,
            "adgroup_name": "Retargeting DK FI",
            "budget": 150.0,
            "location_ids": ["2623032", "660013"],
            "network_types": ["WIFI"],
        }
        return _business_response(request, {"adgroup_id": ADGROUP_ID, "modify_time": "now"})

    requests = _install_business_client(monkeypatch, handler)

    result = await update_adgroup(
        ALIAS,
        ADVERTISER_ID,
        ADGROUP_ID,
        adgroup_name="Retargeting DK FI",
        budget=150.0,
        targeting={"location_ids": ["2623032", "660013"], "network_types": ["WIFI"]},
    )

    assert len(requests) == 1
    assert result["adgroup_id"] == ADGROUP_ID


@pytest.mark.asyncio
async def test_status_and_delete_post_adgroup_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json_body(request)
        if request.url.path == ADGROUP_STATUS_UPDATE_PATH:
            if body["operation_status"] == "DISABLE":
                assert body == {
                    "advertiser_id": ADVERTISER_ID,
                    "adgroup_ids": ["adgroup-1", "adgroup-2"],
                    "operation_status": "DISABLE",
                }
                return _business_response(
                    request,
                    {"success_count": 2, "operation_status": "DISABLE"},
                )
            assert body == {
                "advertiser_id": ADVERTISER_ID,
                "adgroup_ids": ["adgroup-1"],
                "operation_status": "DELETE",
            }
            return _business_response(request, {"success_count": 1, "operation_status": "DELETE"})
        raise AssertionError(f"unexpected path: {request.url.path}")

    requests = _install_business_client(monkeypatch, handler)

    status_result = await update_adgroup_status(
        ALIAS,
        ADVERTISER_ID,
        ["adgroup-1", "adgroup-2"],
        "DISABLE",
    )
    delete_result = await delete_adgroup(ALIAS, ADVERTISER_ID, ["adgroup-1"])

    assert [request.url.path for request in requests] == [
        ADGROUP_STATUS_UPDATE_PATH,
        ADGROUP_DELETE_PATH,
    ]
    assert status_result["success_count"] == 2
    assert delete_result["success_count"] == 1


@pytest.mark.asyncio
async def test_targeting_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    build_calls = _install_forbidden_business_client(monkeypatch)

    invalid_result = await create_adgroup(
        ALIAS,
        ADVERTISER_ID,
        CAMPAIGN_ID,
        "Invalid Geo",
        "PLACEMENT_TYPE_AUTOMATIC",
        "SCHEDULE_START_END",
        "2026-05-23 00:00:00",
        "CPC",
        "CLICK",
        "BID_TYPE_CUSTOM",
        "BUDGET_MODE_DAY",
        100.0,
        {"age_groups": ["AGE_25_34"]},
        promotion_type="WEBSITE",
        schedule_end_time="2026-05-30 00:00:00",
    )

    assert invalid_result["error"] == "validation_error"
    assert "location_ids or zipcode_ids" in str(invalid_result["details"])
    assert build_calls == []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json_body(request)
        assert body["location_ids"] == ["3144096", "2661886"]
        assert "targeting" not in body
        return _business_response(request, {"adgroup_id": ADGROUP_ID})

    requests = _install_business_client(monkeypatch, handler)
    valid_result = await create_adgroup(
        ALIAS,
        ADVERTISER_ID,
        CAMPAIGN_ID,
        "Valid Geo",
        "PLACEMENT_TYPE_AUTOMATIC",
        "SCHEDULE_START_END",
        "2026-05-23 00:00:00",
        "CPC",
        "CLICK",
        "BID_TYPE_CUSTOM",
        "BUDGET_MODE_DAY",
        100.0,
        _targeting(["3144096", "2661886"]),
        promotion_type="WEBSITE",
        schedule_end_time="2026-05-30 00:00:00",
    )

    assert len(requests) == 1
    assert valid_result["adgroup_id"] == ADGROUP_ID


def test_create_adgroup_requires_schedule_start_time() -> None:
    valid_payload: JsonObject = {
        "alias": ALIAS,
        "advertiser_id": ADVERTISER_ID,
        "campaign_id": CAMPAIGN_ID,
        "adgroup_name": "Nordic Prospecting",
        "placement_type": "PLACEMENT_TYPE_AUTOMATIC",
        "schedule_type": "SCHEDULE_START_END",
        "schedule_start_time": "2026-05-23 00:00:00",
        "schedule_end_time": "2026-05-30 00:00:00",
        "billing_event": "CPC",
        "optimization_goal": "CLICK",
        "bid_type": "BID_TYPE_CUSTOM",
        "budget_mode": "BUDGET_MODE_DAY",
        "budget": 100.0,
        "targeting": {"location_ids": ["3144096"]},
    }

    missing_start_time = dict(valid_payload)
    del missing_start_time["schedule_start_time"]
    with pytest.raises(ValidationError) as missing_error:
        _ = CreateAdGroupRequest.model_validate(missing_start_time)
    assert "schedule_start_time" in str(missing_error.value)

    invalid_start_time = dict(valid_payload)
    invalid_start_time["schedule_start_time"] = 123
    with pytest.raises(ValidationError) as type_error:
        _ = CreateAdGroupRequest.model_validate(invalid_start_time)
    assert "schedule_start_time" in str(type_error.value)


@pytest.mark.asyncio
async def test_all_adgroup_tools_advertise_destructive_hint() -> None:
    registered_tools = {tool.name: tool for tool in await app.list_tools()}

    for tool_name in (
        "create_adgroup",
        "update_adgroup",
        "update_adgroup_status",
        "delete_adgroup",
    ):
        tool = registered_tools[tool_name]
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is True
        fn = getattr(adgroup_tools, tool_name)
        assert getattr(fn, "__tiktok_mcp_destructive__", False) is True
        assert getattr(fn, "__tiktok_mcp_write_api__", None) == "marketing"


def _set_writes_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("TIKTOK_MCP_ALLOW_WRITES", raising=False)
        monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
        return
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", value)
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")


def _targeting(location_ids: list[str]) -> JsonObject:
    return {"location_ids": location_ids}


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

    monkeypatch.setattr(adgroup_tools, "_build_business_client", build_client)
    return requests


def _install_forbidden_business_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    build_calls: list[str] = []

    async def build_client(alias: str) -> BusinessAPIClient:
        build_calls.append(alias)
        raise AssertionError("BusinessAPIClient must not be built after validation failure")

    monkeypatch.setattr(adgroup_tools, "_build_business_client", build_client)
    return build_calls


def _business_response(request: httpx.Request, data: JsonObject) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "message": "OK", "request_id": "req-adgroup-ok", "data": data},
        request=request,
    )


def _json_body(request: httpx.Request) -> JsonObject:
    return cast(JsonObject, json.loads(request.content.decode("utf-8")))


def _account() -> AccountWithTokens:
    return AccountWithTokens(
        alias=ALIAS,
        api_type=ApiType.MARKETING,
        sandbox=True,
        tiktok_id="marketing-tiktok-id",
        display_name="Marketing Demo",
        avatar_url=None,
        scopes=["business.ad.write"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
        access_token=SecretStr("adgroup-access-token"),
        refresh_token=SecretStr("adgroup-refresh-token"),
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
