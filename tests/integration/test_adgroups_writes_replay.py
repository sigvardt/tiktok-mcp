from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false, reportAttributeAccessIssue=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportUnknownMemberType=false, reportExplicitAny=false, reportAny=false
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import vcr  # type: ignore[import-untyped]
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.tools import marketing_writes_adgroups as adgroup_tools
from tiktok_mcp.tools.marketing_writes_adgroups import (
    ADGROUP_CREATE_PATH,
    ADGROUP_DELETE_PATH,
    ADGROUP_STATUS_UPDATE_PATH,
    ADGROUP_UPDATE_PATH,
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
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes" / "marketing_adgroups"
ADGROUPS_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Access-Token", "REDACTED")],
)


@pytest.mark.asyncio
async def test_adgroup_crud_happy_path_with_cassettes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    requests = _install_business_client(monkeypatch, _cassette_handler)

    created = await create_adgroup(
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
        {"location_ids": ["3144096", "2661886"], "age_groups": ["AGE_25_34"]},
        promotion_type="WEBSITE",
        schedule_end_time="2026-05-30 00:00:00",
        bid_price=2.5,
        audience_ids=["audience-1"],
    )
    updated = await update_adgroup(
        ALIAS,
        ADVERTISER_ID,
        ADGROUP_ID,
        adgroup_name="Nordic Retargeting",
        budget=150.0,
        targeting={"location_ids": ["2623032", "660013"]},
    )
    paused = await update_adgroup_status(
        ALIAS,
        ADVERTISER_ID,
        ["adgroup-1", "adgroup-2"],
        "DISABLE",
    )
    deleted = await delete_adgroup(ALIAS, ADVERTISER_ID, ["adgroup-1", "adgroup-2"])

    assert created["adgroup_id"] == ADGROUP_ID
    assert updated["adgroup_id"] == ADGROUP_ID
    assert paused["success_count"] == 2
    assert deleted["success_count"] == 2
    assert [request.url.path for request in requests] == [
        ADGROUP_CREATE_PATH,
        ADGROUP_UPDATE_PATH,
        ADGROUP_STATUS_UPDATE_PATH,
        ADGROUP_DELETE_PATH,
    ]
    status_body = _json_body(requests[2])
    assert status_body["operation_status"] == "DISABLE"
    assert status_body["adgroup_ids"] == ["adgroup-1", "adgroup-2"]


def test_adgroup_vcr_config_scrubs_access_token() -> None:
    assert ADGROUPS_VCR.filter_headers == [("Access-Token", "REDACTED")]


def test_adgroup_cassettes_keep_access_token_redacted() -> None:
    for cassette_path in CASSETTE_DIR.glob("*.yaml"):
        cassette_text = cassette_path.read_text(encoding="utf-8")
        assert "adgroup-access-token" not in cassette_text
        assert "Access-Token" in cassette_text
        assert "REDACTED" in cassette_text


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


def _cassette_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == ADGROUP_STATUS_UPDATE_PATH:
        operation_status = _json_body(request)["operation_status"]
        cassette_name = "delete.yaml" if operation_status == "DELETE" else "pause.yaml"
        return _cassette_response(cassette_name, request)
    cassette_name_by_path = {
        ADGROUP_CREATE_PATH: "create.yaml",
        ADGROUP_UPDATE_PATH: "update.yaml",
    }
    cassette_name = cassette_name_by_path[request.url.path]
    return _cassette_response(cassette_name, request)


def _cassette_response(name: str, request: httpx.Request) -> httpx.Response:
    yaml = pytest.importorskip("yaml")
    payload = yaml.safe_load((CASSETTE_DIR / name).read_text(encoding="utf-8"))
    interaction = cast(dict[str, Any], payload["interactions"][0])
    response = cast(dict[str, Any], interaction["response"])
    body = cast(dict[str, object], response["body"])
    raw_body = body.get("string", "")
    content = raw_body.encode("utf-8") if isinstance(raw_body, str) else cast(bytes, raw_body)
    status = cast(dict[str, object], response["status"])
    status_code = status["code"]
    if not isinstance(status_code, int):
        raise TypeError("cassette status code must be an integer")
    return httpx.Response(
        status_code,
        content=content,
        headers=_single_value_headers(cast(Mapping[str, object], response.get("headers", {}))),
        request=request,
    )


def _single_value_headers(raw_headers: Mapping[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, list) and value:
            headers[str(key)] = str(value[0])
        elif value is not None:
            headers[str(key)] = str(value)
    return headers


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
