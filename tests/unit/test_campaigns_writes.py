# pyright: reportMissingTypeStubs=false, reportAny=false, reportPrivateUsage=false, reportUnknownArgumentType=false
# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.server import app
from tiktok_mcp.tools import marketing_writes_campaigns as campaign_tools
from tiktok_mcp.tools.marketing_writes_campaigns import (
    CAMPAIGN_CREATE_PATH,
    create_campaign,
    delete_campaign,
    update_campaign,
    update_campaign_status,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "7642629596042543111"
CAMPAIGN_ID = "1733456789012345"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env_value",
    (None, "", "0", "false", "False", "no", "display,comments"),
)
async def test_create_campaign_blocked_when_writes_disabled(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)
    if env_value is None:
        monkeypatch.delenv("TIKTOK_MCP_ALLOW_WRITES", raising=False)
    else:
        monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", env_value)

    result = await create_campaign(
        ALIAS,
        ADVERTISER_ID,
        "QA-TEST",
        "TRAFFIC",
        "BUDGET_MODE_DAY",
        50,
    )

    assert result["error"] == "writes_disabled"
    assert result["api"] == "marketing"
    would_have_done = cast(dict[str, object], result["would_have_done"])
    assert would_have_done["endpoint"] == CAMPAIGN_CREATE_PATH
    assert would_have_done["advertiser_id"] == ADVERTISER_ID
    assert build_calls == []


@pytest.mark.asyncio
async def test_create_campaign_routes_marketing_only(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "comments")

    result = await create_campaign(
        ALIAS,
        ADVERTISER_ID,
        "QA-TEST",
        "TRAFFIC",
        "BUDGET_MODE_DAY",
        50,
    )

    assert result["error"] == "writes_disabled"
    assert result["api"] == "marketing"
    would_have_done = cast(dict[str, object], result["would_have_done"])
    assert would_have_done["endpoint"] == CAMPAIGN_CREATE_PATH
    assert build_calls == []


@pytest.mark.asyncio
async def test_create_campaign_posts_with_access_token_and_logs_metadata(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_business_client(
        monkeypatch,
        lambda request: _business_response(
            request,
            {
                "campaign_id": CAMPAIGN_ID,
                "modify_time": "2026-05-22 12:00:00",
                "operation_status": "ENABLE",
                "request_id": "req-campaign-create",
            },
        ),
    )
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")

    with caplog.at_level(logging.INFO, logger="tiktok_mcp.tools.marketing_writes_campaigns"):
        result = await create_campaign(
            ALIAS,
            ADVERTISER_ID,
            "QA-TEST",
            "TRAFFIC",
            "BUDGET_MODE_DAY",
            50,
            special_industries=["HOUSING"],
        )

    assert result == {
        "campaign_id": CAMPAIGN_ID,
        "modify_time": "2026-05-22 12:00:00",
        "status": "ENABLE",
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == CAMPAIGN_CREATE_PATH
    assert request.headers["Access-Token"] == "marketing-access-token"
    assert "authorization" not in request.headers
    body = json.loads(request.content.decode("utf-8"))
    assert body == {
        "advertiser_id": ADVERTISER_ID,
        "campaign_name": "QA-TEST",
        "objective_type": "TRAFFIC",
        "budget_mode": "BUDGET_MODE_DAY",
        "budget": 50.0,
        "special_industries": ["HOUSING"],
    }
    assert any(record.action == "campaign.create" for record in caplog.records)
    assert any(record.campaign_id == CAMPAIGN_ID for record in caplog.records)


@pytest.mark.asyncio
async def test_invalid_create_campaign_returns_validation_error_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")

    result = await create_campaign(
        ALIAS,
        ADVERTISER_ID,
        "QA-TEST",
        "TRAFFIC",
        "BUDGET_MODE_DAY",
        0,
    )

    assert result["error"] == "validation_error"
    assert build_calls == []


def test_campaign_write_tools_destructive_hint_registry_introspection() -> None:
    tools = app._tool_manager.__dict__["_tools"]

    for tool_name in (
        "create_campaign",
        "update_campaign",
        "update_campaign_status",
        "delete_campaign",
    ):
        tool = tools[tool_name]
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is True


def test_campaign_write_tools_are_marked_for_marketing_writes() -> None:
    for tool in (create_campaign, update_campaign, update_campaign_status, delete_campaign):
        assert getattr(tool, "__tiktok_mcp_destructive__", False) is True
        assert getattr(tool, "__tiktok_mcp_write_api__", None) == "marketing"


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

    monkeypatch.setattr(campaign_tools, "_build_business_client", build_client)
    return requests


def _install_forbidden_business_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    build_calls: list[str] = []

    async def build_client(alias: str) -> BusinessAPIClient:
        build_calls.append(alias)
        raise AssertionError("BusinessAPIClient must not be built for blocked/invalid writes")

    monkeypatch.setattr(campaign_tools, "_build_business_client", build_client)
    return build_calls


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
        scopes=["business.campaign.write"],
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
