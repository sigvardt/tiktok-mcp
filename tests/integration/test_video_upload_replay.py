from __future__ import annotations

# pyright: reportAny=false, reportAttributeAccessIssue=false, reportExplicitAny=false
# pyright: reportMissingImports=false, reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnannotatedClassAttribute=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
import base64
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import vcr  # type: ignore[import-untyped]
from pydantic import SecretStr

from tiktok_mcp.api.posting import PostingAPIClient
from tiktok_mcp.api.posting.chunker import MIN_CHUNK_BYTES, iter_file_chunks
from tiktok_mcp.auth.keychain import account_key, app_creds_key, serialize_account_record
from tiktok_mcp.tools import posting_writes_video_upload as upload_tools
from tiktok_mcp.tools.posting_writes_video_upload import (
    finalize_video_upload,
    init_video_upload,
    upload_video_chunk,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType

ALIAS = "posting-upload-alias"
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes" / "posting_video_upload"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "posting" / "sample_8mb.mp4"
UPLOAD_URL = "https://upload.example.test/upload/chunk"
POSTING_UPLOAD_FILTER_HEADERS = [("Authorization", "REDACTED")]
POSTING_UPLOAD_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=POSTING_UPLOAD_FILTER_HEADERS,
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


class PostingClientFactory:
    def __init__(
        self,
        backend: MemoryBackend,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self.backend = backend
        self.handler = handler
        self.http_clients: list[httpx.AsyncClient] = []

    def __call__(self) -> PostingAPIClient:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        self.http_clients.append(http_client)
        return PostingAPIClient(backend=self.backend, http_client=http_client)

    async def aclose(self) -> None:
        for http_client in self.http_clients:
            await http_client.aclose()


class CassetteReplay:
    def __init__(self, name: str) -> None:
        yaml = pytest.importorskip("yaml")
        payload = yaml.safe_load((CASSETTE_DIR / name).read_text(encoding="utf-8"))
        self.interactions = cast(list[dict[str, Any]], payload["interactions"])
        self.index = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        interaction = self.interactions[self.index]
        self.index += 1
        cassette_request = cast(dict[str, Any], interaction["request"])
        expected_uri = httpx.URL(cast(str, cassette_request["uri"]))
        assert request.method == cassette_request["method"]
        assert request.url.path == expected_uri.path

        response = cast(dict[str, Any], interaction["response"])
        body = cast(dict[str, object], response.get("body", {}))
        raw_body = body.get("string", "")
        content = raw_body.encode("utf-8") if isinstance(raw_body, str) else cast(bytes, raw_body)
        status = cast(dict[str, object], response["status"])
        status_code = status["code"]
        if not isinstance(status_code, int):
            raise TypeError("cassette status code must be an integer")
        return httpx.Response(
            status_code,
            content=content,
            headers=_single_value_headers(cast(dict[str, object], response.get("headers", {}))),
            request=request,
        )


@pytest.fixture(autouse=True)
def reset_upload_sessions() -> Iterator[None]:
    upload_tools._UPLOAD_SESSIONS.clear()
    yield
    upload_tools._UPLOAD_SESSIONS.clear()


@pytest.mark.asyncio
async def test_file_upload_happy_path_replays_cassette(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    backend = MemoryBackend()
    await _store_account(backend)
    replay = CassetteReplay("single_chunk_draft.yaml")
    factory = PostingClientFactory(backend, replay)
    monkeypatch.setattr(upload_tools, "_build_posting_client", factory)

    try:
        init_response = await init_video_upload(ALIAS, 8 * 1024 * 1024, 8 * 1024 * 1024, 1)
        chunks = [
            chunk
            async for chunk in iter_file_chunks(
                FIXTURE_PATH,
                file_size=8 * 1024 * 1024,
                chunk_size=8 * 1024 * 1024,
                total_chunk_count=1,
            )
        ]
        upload_response = await upload_video_chunk(
            "publish-8mb",
            UPLOAD_URL,
            0,
            base64.b64encode(chunks[0]).decode("ascii"),
        )
        final_response = await finalize_video_upload("publish-8mb")
    finally:
        await factory.aclose()

    assert init_response == {"publish_id": "publish-8mb", "upload_url": UPLOAD_URL}
    assert upload_response["status"] == "CHUNK_UPLOADED"
    assert final_response["status"] == "PUBLISH_COMPLETE"
    assert replay.index == len(replay.interactions)
    assert json.loads(replay.requests[0].content) == {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": 8 * 1024 * 1024,
            "chunk_size": 8 * 1024 * 1024,
            "total_chunk_count": 1,
        }
    }
    assert replay.requests[1].headers["content-range"] == "bytes 0-8388607/8388608"


@pytest.mark.asyncio
async def test_token_refresh_mid_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    backend = MemoryBackend()
    await _store_account(backend)
    await _store_app_credentials(backend)
    replay = CassetteReplay("token_refresh_mid_chunk.yaml")
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(replay))
    try:
        async with PostingAPIClient(backend=backend, http_client=http_client) as client:
            _ = await client.put_chunk_to_url(
                ALIAS,
                UPLOAD_URL,
                headers={"Content-Range": f"bytes 0-{MIN_CHUNK_BYTES - 1}/{MIN_CHUNK_BYTES}"},
                content=b"x" * MIN_CHUNK_BYTES,
            )
    finally:
        await http_client.aclose()

    put_authorizations = [
        request.headers.get("authorization")
        for request in replay.requests
        if request.method == "PUT"
    ]
    assert put_authorizations == [
        "Bearer posting-access",
        "Bearer cassette-fresh-access",
    ]
    stored = await backend.get(account_key(ApiType.CONTENT_POSTING, False, ALIAS))
    assert stored is not None
    assert "cassette-fresh-refresh" in stored


def test_posting_upload_vcr_config_scrubs_authorization() -> None:
    assert POSTING_UPLOAD_FILTER_HEADERS == [("Authorization", "REDACTED")]


def _single_value_headers(raw_headers: dict[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, list) and value:
            headers[str(key)] = str(value[0])
        elif value is not None:
            headers[str(key)] = str(value)
    return headers


async def _store_account(backend: MemoryBackend) -> None:
    account = Account(
        alias=ALIAS,
        api_type=ApiType.CONTENT_POSTING,
        sandbox=False,
        tiktok_id="posting-open-id",
        display_name="Posting Creator",
        avatar_url=None,
        scopes=["video.upload"],
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


async def _store_app_credentials(backend: MemoryBackend) -> None:
    payload = {
        "api_type": ApiType.CONTENT_POSTING.value,
        "sandbox": False,
        "client_id": "posting-client-id",
        "client_secret": "posting-client-secret",
        "created_at": datetime.now(UTC).isoformat(),
    }
    await backend.set(app_creds_key(ApiType.CONTENT_POSTING, False), json.dumps(payload))
