from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false, reportAttributeAccessIssue=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportAny=false, reportExplicitAny=false
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
import vcr  # type: ignore[import-untyped]
from pydantic import SecretStr

from tiktok_mcp.api.posting.client import BASE_URL, POST_STATUS_PATH, PostingAPIClient
from tiktok_mcp.auth.keychain import account_key, serialize_account_record
from tiktok_mcp.tools import posting_writes_pull_and_photo as posting_tools
from tiktok_mcp.tools.posting_writes_pull_and_photo import (
    CANCEL_PUBLISH_PATH,
    INBOX_VIDEO_INIT_PATH,
    PHOTO_INIT_PATH,
    cancel_publish,
    upload_photo_from_urls,
    upload_video_from_url,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType

ALIAS = "posting-alias"
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes" / "posting_pull"
POSTING_PULL_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Authorization", "REDACTED")],
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
def clear_publish_aliases() -> None:
    posting_tools._PUBLISH_ALIASES.clear()


@pytest.mark.asyncio
async def test_pull_from_url_draft_and_status_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    replay = _patch_posting_client(monkeypatch, backend, "from_url_draft.yaml")
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    init_result = await upload_video_from_url(ALIAS, "https://example.com/sample.mp4")
    status_result = await posting_tools.get_publish_status(cast(str, init_result["publish_id"]))

    assert init_result["publish_id"] == "v_inbox_url~v2.123"
    assert status_result["status"] == "FETCH_IN_PROGRESS"
    assert [request.url.path for request in replay.requests] == [
        INBOX_VIDEO_INIT_PATH,
        POST_STATUS_PATH,
    ]
    init_body = _json_body(replay.requests[0])
    assert init_body == {
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": "https://example.com/sample.mp4",
        }
    }
    assert replay.requests[0].headers["authorization"] == "Bearer posting-access"


@pytest.mark.asyncio
async def test_photo_carousel_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    replay = _patch_posting_client(monkeypatch, backend, "photo_carousel.yaml")
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    result = await upload_photo_from_urls(
        ALIAS,
        [
            "https://example.com/photo-1.webp",
            "https://example.com/photo-2.webp",
            "https://example.com/photo-3.webp",
        ],
    )

    assert result["publish_id"] == "p_photo_url~v2.456"
    assert replay.requests[0].url.path == PHOTO_INIT_PATH
    body = _json_body(replay.requests[0])
    source_info = cast(dict[str, object], body["source_info"])
    photo_images = cast(dict[str, object], source_info["photo_images"])
    assert body["post_mode"] == "MEDIA_UPLOAD"
    assert len(cast(list[str], photo_images["image_urls"])) == 3
    assert "post_info" not in body


@pytest.mark.asyncio
async def test_cancel_pending_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    replay = _patch_posting_client(monkeypatch, backend, "cancel_pending.yaml")
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    await posting_tools._remember_publish_alias("v_inbox_url~v2.cancel", ALIAS)

    result = await cancel_publish("v_inbox_url~v2.cancel")

    assert result["cancelled"] is True
    assert [request.url.path for request in replay.requests] == [
        POST_STATUS_PATH,
        CANCEL_PUBLISH_PATH,
    ]
    assert _json_body(replay.requests[1]) == {"publish_id": "v_inbox_url~v2.cancel"}


def test_vcr_configuration_points_at_posting_pull_cassettes() -> None:
    assert POSTING_PULL_VCR.cassette_library_dir == str(CASSETTE_DIR)


class CassetteReplay:
    def __init__(self, cassette_name: str) -> None:
        self.interactions: list[dict[str, object]] = _cassette_interactions(cassette_name)
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        index = len(self.requests)
        self.requests.append(request)
        interaction = self.interactions[index]
        response = cast(dict[str, object], interaction["response"])
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


def _patch_posting_client(
    monkeypatch: pytest.MonkeyPatch,
    backend: MemoryBackend,
    cassette_name: str,
) -> CassetteReplay:
    replay = CassetteReplay(cassette_name)

    def build_client() -> PostingAPIClient:
        return PostingAPIClient(
            backend=backend,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(replay.handle),
                base_url=BASE_URL,
            ),
        )

    monkeypatch.setattr(posting_tools, "_build_posting_client", build_client)
    return replay


async def _store_account(backend: MemoryBackend) -> None:
    account = Account(
        alias=ALIAS,
        api_type=ApiType.CONTENT_POSTING,
        sandbox=False,
        tiktok_id="posting-open-id",
        display_name="Posting Creator",
        avatar_url=None,
        scopes=["user.info.basic", "video.upload", "video.publish"],
        created_at=datetime.now(UTC),
        last_used_at=None,
        status=AccountStatus.OK,
    )
    tokens = AccountTokens(
        access_token=SecretStr("posting-access"),
        refresh_token=SecretStr("posting-refresh"),
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
        last_rotated_at=datetime.now(UTC),
    )
    await backend.set(
        account_key(account.api_type, account.sandbox, account.alias),
        serialize_account_record(account, tokens),
    )


def _cassette_interactions(cassette_name: str) -> list[dict[str, object]]:
    yaml = pytest.importorskip("yaml")
    payload = yaml.safe_load((CASSETTE_DIR / cassette_name).read_text(encoding="utf-8"))
    interactions = cast(dict[str, object], payload)["interactions"]
    if not isinstance(interactions, list):
        raise TypeError("cassette interactions must be a list")
    return [cast(dict[str, object], interaction) for interaction in interactions]


def _single_value_headers(raw_headers: Mapping[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, list) and value:
            headers[str(key)] = str(value[0])
        elif value is not None:
            headers[str(key)] = str(value)
    return headers


def _json_body(request: httpx.Request) -> dict[str, object]:
    payload = cast(object, json.loads(request.content.decode()))
    if not isinstance(payload, dict):
        raise TypeError("request body must be a JSON object")
    return {str(key): value for key, value in payload.items()}
