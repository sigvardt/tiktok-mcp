from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
import vcr  # type: ignore[import-untyped]
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.tools import marketing_writes_creatives as creative_tools
from tiktok_mcp.tools.marketing_writes_creatives import (
    IMAGE_UPLOAD_PATH,
    VIDEO_UPLOAD_PATH,
    upload_image_asset,
    upload_video_asset,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "7642629596042543111"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "creatives"
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes" / "marketing_creatives"
CREATIVES_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Access-Token", "REDACTED")],
)


@pytest.mark.asyncio
async def test_upload_image_asset_replay_cassette(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    requests = _install_replay_client(monkeypatch, "upload_image.yaml")

    result = await upload_image_asset(
        ALIAS,
        ADVERTISER_ID,
        str(FIXTURE_DIR / "sample.jpg"),
    )

    assert CREATIVES_VCR is not None
    assert result["image_id"] == "img-replay-1"
    assert result["format"] == "JPG"
    assert len(requests) == 1
    assert requests[0].url.path == IMAGE_UPLOAD_PATH
    assert requests[0].headers["Access-Token"] == "marketing-access-token"


@pytest.mark.asyncio
async def test_upload_video_asset_replay_cassette_has_multiple_chunk_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    requests = _install_replay_client(monkeypatch, "upload_video_chunked.yaml")

    result = await upload_video_asset(
        ALIAS,
        ADVERTISER_ID,
        str(FIXTURE_DIR / "sample_8mb.mp4"),
    )

    assert result["video_id"] == "vid-replay-1"
    assert result["format"] == "MP4"
    assert len(requests) >= 2
    assert [request.url.path for request in requests] == [VIDEO_UPLOAD_PATH, VIDEO_UPLOAD_PATH]


def _install_replay_client(
    monkeypatch: pytest.MonkeyPatch,
    cassette_name: str,
) -> list[httpx.Request]:
    interactions = _cassette_interactions(cassette_name)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        interaction_index = len(requests) - 1
        return _response_from_interaction(interactions[interaction_index], request)

    async def build_client(alias: str) -> BusinessAPIClient:
        assert alias == ALIAS
        return BusinessAPIClient(
            _account(),
            _credentials(),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(creative_tools, "_build_business_client", build_client)
    return requests


def _cassette_interactions(cassette_name: str) -> list[Mapping[str, object]]:
    yaml = pytest.importorskip("yaml")
    payload = yaml.safe_load((CASSETTE_DIR / cassette_name).read_text(encoding="utf-8"))
    interactions = cast(list[Mapping[str, object]], payload["interactions"])
    return interactions


def _response_from_interaction(
    interaction: Mapping[str, object],
    request: httpx.Request,
) -> httpx.Response:
    response = cast(Mapping[str, object], interaction["response"])
    body = cast(Mapping[str, object], response["body"])
    status = cast(Mapping[str, object], response["status"])
    headers = _single_value_headers(cast(Mapping[str, object], response.get("headers", {})))
    status_code = status["code"]
    if not isinstance(status_code, int):
        raise TypeError("cassette status code must be an integer")
    raw_body = body.get("string", "")
    content = raw_body.encode("utf-8") if isinstance(raw_body, str) else cast(bytes, raw_body)
    return httpx.Response(status_code, content=content, headers=headers, request=request)


def _single_value_headers(raw_headers: Mapping[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, list) and value:
            headers[str(key)] = str(value[0])
        elif value is not None:
            headers[str(key)] = str(value)
    return headers


def _account() -> AccountWithTokens:
    return AccountWithTokens(
        alias=ALIAS,
        api_type=ApiType.MARKETING,
        sandbox=True,
        tiktok_id=ADVERTISER_ID,
        display_name="Marketing Demo",
        avatar_url=None,
        scopes=["business.creative.write"],
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
