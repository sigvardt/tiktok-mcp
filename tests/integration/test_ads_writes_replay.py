from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false, reportExplicitAny=false
# pyright: reportUnknownMemberType=false
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
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes" / "marketing_ads"

ADS_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Access-Token", "REDACTED")],
)


@pytest.mark.asyncio
async def test_ad_crud_write_replay_cassettes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")

    def handler(request: httpx.Request) -> httpx.Response:
        return _cassette_response(_cassette_name(request.url.path), request)

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
    updated = await update_ad(ALIAS, ADVERTISER_ID, AD_ID, ad_name="Updated single video")
    status = await update_ad_status(ALIAS, ADVERTISER_ID, AD_ID, "DISABLE")
    deleted = await delete_ad(ALIAS, ADVERTISER_ID, AD_ID)

    assert created["ad_id"] == AD_ID
    assert updated["ad_id"] == AD_ID
    assert status == {"ad_ids": [AD_ID], "operation_status": "DISABLE"}
    assert deleted == {"ad_ids": [AD_ID], "deleted": True}
    assert [request.url.path for request in requests] == [
        AD_CREATE_PATH,
        AD_UPDATE_PATH,
        AD_STATUS_UPDATE_PATH,
        AD_DELETE_PATH,
    ]
    assert _json_body(requests[0])["creative_material_mode"] == "CUSTOM"
    assert _json_body(requests[0])["video_id"] == "video-123"


def test_marketing_ads_vcr_config_scrubs_access_token() -> None:
    assert ADS_VCR.filter_headers == [("Access-Token", "REDACTED")]


def test_marketing_ads_cassettes_scrub_access_token() -> None:
    for cassette_path in CASSETTE_DIR.glob("*.yaml"):
        cassette_text = cassette_path.read_text(encoding="utf-8")
        assert "marketing-access-token" not in cassette_text
        assert "Access-Token:" in cassette_text
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

    monkeypatch.setattr(ads_tools, "_build_business_client", build_client)
    return requests


def _cassette_name(path: str) -> str:
    return {
        AD_CREATE_PATH: "create_single_video.yaml",
        AD_UPDATE_PATH: "update_ad.yaml",
        AD_STATUS_UPDATE_PATH: "update_ad_status.yaml",
        AD_DELETE_PATH: "delete_ad.yaml",
    }[path]


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


def _json_body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content.decode("utf-8")))


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
