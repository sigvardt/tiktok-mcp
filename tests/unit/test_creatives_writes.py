from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false, reportUnknownMemberType=false
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import httpx
import pytest
from mcp.types import ToolAnnotations
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.marketing.asset_chunker import DEFAULT_CHUNK_SIZE, chunk_file, sha256_file
from tiktok_mcp.server import app
from tiktok_mcp.tools import marketing_writes_creatives as creative_tools
from tiktok_mcp.tools.marketing_writes_creatives import (
    IMAGE_DELETE_PATH,
    IMAGE_UPLOAD_PATH,
    VIDEO_DELETE_PATH,
    VIDEO_UPLOAD_PATH,
    delete_image_asset,
    delete_video_asset,
    upload_image_asset,
    upload_video_asset,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "7642629596042543111"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "creatives"
SAMPLE_IMAGE = FIXTURE_DIR / "sample.jpg"
SAMPLE_VIDEO = FIXTURE_DIR / "sample_8mb.mp4"
CREATIVE_TOOL_NAMES = (
    "upload_video_asset",
    "upload_image_asset",
    "delete_video_asset",
    "delete_image_asset",
)


class RegisteredTool(Protocol):
    annotations: ToolAnnotations


class ToolManager(Protocol):
    _tools: Mapping[str, RegisteredTool]


class FastMCPWithToolManager(Protocol):
    _tool_manager: ToolManager


class CreativeWriteTool(Protocol):
    __tiktok_mcp_destructive__: bool
    __tiktok_mcp_write_api__: str


@pytest.mark.asyncio
async def test_blocked_tools_return_structured_writes_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    blocked_calls = [
        await upload_image_asset(ALIAS, ADVERTISER_ID, "missing.jpg"),
        await upload_video_asset(ALIAS, ADVERTISER_ID, "missing.mp4"),
        await delete_video_asset(ALIAS, ADVERTISER_ID, ["video-1"]),
        await delete_image_asset(ALIAS, ADVERTISER_ID, ["image-1"]),
    ]

    assert [result["error"] for result in blocked_calls] == ["writes_disabled"] * 4
    assert [result["api"] for result in blocked_calls] == ["marketing"] * 4


def test_chunking_splits_file_larger_than_5mb() -> None:
    chunks = list(chunk_file(SAMPLE_VIDEO, DEFAULT_CHUNK_SIZE))

    assert len(chunks) >= 2
    assert len(chunks[0]) == DEFAULT_CHUNK_SIZE
    assert sum(len(chunk) for chunk in chunks) == SAMPLE_VIDEO.stat().st_size


def test_creative_signatures_are_raw_file_sha256() -> None:
    expected = _manual_sha256(SAMPLE_IMAGE)

    assert sha256_file(SAMPLE_IMAGE) == expected


def test_all_creative_tools_are_destructive_and_write_gated() -> None:
    registered_tools = cast(FastMCPWithToolManager, cast(object, app))._tool_manager._tools

    for tool_name in CREATIVE_TOOL_NAMES:
        tool = registered_tools[tool_name]
        assert tool.annotations.destructiveHint is True
        tool_function = cast(CreativeWriteTool, getattr(creative_tools, tool_name))
        assert tool_function.__tiktok_mcp_destructive__ is True
        assert tool_function.__tiktok_mcp_write_api__ == "marketing"


@pytest.mark.asyncio
async def test_upload_image_posts_single_multipart_with_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    signature = sha256_file(SAMPLE_IMAGE)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == IMAGE_UPLOAD_PATH
        assert request.headers["Access-Token"] == "marketing-access-token"
        assert signature.encode("utf-8") in request.content
        assert b'name="image_file"; filename="sample.jpg"' in request.content
        return _business_response(
            request,
            {
                "image_id": "img-asset-1",
                "image_url": "https://p16-ad-sg.tiktokcdn.com/sample.jpg",
                "image_signature": signature,
                "size": SAMPLE_IMAGE.stat().st_size,
                "format": "JPG",
                "height": 240,
                "width": 320,
            },
        )

    requests = _install_business_client(monkeypatch, handler)

    result = await upload_image_asset(ALIAS, ADVERTISER_ID, str(SAMPLE_IMAGE))

    assert result["image_id"] == "img-asset-1"
    assert result["format"] == "JPG"
    assert result["signature"] == signature
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_upload_video_dedup_response_returns_existing_id_without_reupload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    signature = sha256_file(SAMPLE_VIDEO)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == VIDEO_UPLOAD_PATH
        assert signature.encode("utf-8") in request.content
        assert b'name="chunk_index"' in request.content
        return _business_response(
            request,
            {
                "existing_video_id": "vid-existing",
                "video_signature": signature,
                "size": SAMPLE_VIDEO.stat().st_size,
                "format": "MP4",
                "height": 1080,
                "width": 1920,
                "bit_rate": 4000,
                "duration": 12.5,
                "file_name": SAMPLE_VIDEO.name,
            },
        )

    requests = _install_business_client(monkeypatch, handler)

    result = await upload_video_asset(ALIAS, ADVERTISER_ID, str(SAMPLE_VIDEO))

    assert result["video_id"] == "vid-existing"
    assert result["video_signature"] == signature
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_delete_video_and_image_assets_post_id_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content.decode("utf-8")))
        assert body["advertiser_id"] == ADVERTISER_ID
        if request.url.path == VIDEO_DELETE_PATH:
            assert body["video_ids"] == ["video-1", "video-2"]
            return _business_response(request, {"video_ids": ["video-1", "video-2"]})
        if request.url.path == IMAGE_DELETE_PATH:
            assert body["image_ids"] == ["image-1"]
            return _business_response(request, {"image_ids": ["image-1"]})
        raise AssertionError(f"unexpected path {request.url.path}")

    requests = _install_business_client(monkeypatch, handler)

    video_result = await delete_video_asset(ALIAS, ADVERTISER_ID, ["video-1", "video-2"])
    image_result = await delete_image_asset(ALIAS, ADVERTISER_ID, ["image-1"])

    assert video_result["deleted"] is True
    assert image_result["deleted"] is True
    assert [request.url.path for request in requests] == [VIDEO_DELETE_PATH, IMAGE_DELETE_PATH]


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

    monkeypatch.setattr(creative_tools, "_build_business_client", build_client)
    return requests


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


def _manual_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
