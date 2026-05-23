from __future__ import annotations

# pyright: reportAttributeAccessIssue=false, reportMissingImports=false, reportMissingTypeStubs=false
# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
import base64
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast

import httpx
import pytest
from pydantic import SecretStr

from tiktok_mcp.api.posting.chunker import MAX_CHUNK_BYTES, MIN_CHUNK_BYTES, chunk_bytes_for_upload
from tiktok_mcp.api.posting.client import OAUTH_TOKEN_PATH, PostingAPIClient
from tiktok_mcp.auth.keychain import account_key, app_creds_key, serialize_account_record
from tiktok_mcp.tools import posting_writes_video_upload as upload_tools
from tiktok_mcp.tools.posting_writes_video_upload import (
    INIT_VIDEO_UPLOAD_PATH,
    init_video_upload,
    upload_video_chunk,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType

ALIAS = "posting-upload-alias"
UPLOAD_URL = "https://upload.example.test/upload/chunk"


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
def reset_upload_sessions() -> Iterator[None]:
    upload_tools._UPLOAD_SESSIONS.clear()
    yield
    upload_tools._UPLOAD_SESSIONS.clear()


@pytest.mark.asyncio
async def test_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    response = cast(
        dict[str, object],
        await init_video_upload(ALIAS, MIN_CHUNK_BYTES, MIN_CHUNK_BYTES, 1),
    )

    assert response["error"] == "writes_disabled"
    assert response["api"] == "posting"
    assert response["tool"] == "init_video_upload"


def test_chunk_math() -> None:
    eight_mb = 8 * 1024 * 1024
    seventy_mb = 70 * 1024 * 1024

    single_chunk = chunk_bytes_for_upload(eight_mb, eight_mb, 1)
    two_chunks = chunk_bytes_for_upload(seventy_mb, MAX_CHUNK_BYTES, 2)

    assert [(chunk.start, chunk.end, chunk.size) for chunk in single_chunk] == [
        (0, eight_mb - 1, eight_mb)
    ]
    assert [chunk.size for chunk in two_chunks] == [MAX_CHUNK_BYTES, 6 * 1024 * 1024]
    assert two_chunks[0].content_range == f"bytes 0-{MAX_CHUNK_BYTES - 1}/{seventy_mb}"
    assert two_chunks[1].content_range == f"bytes {MAX_CHUNK_BYTES}-{seventy_mb - 1}/{seventy_mb}"


@pytest.mark.asyncio
async def test_idempotent_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    fake_client = FakePostingClient()
    monkeypatch.setattr(upload_tools, "_build_posting_client", lambda: fake_client)
    chunk = b"x" * MIN_CHUNK_BYTES
    chunk_b64 = base64.b64encode(chunk).decode("ascii")

    init_response = await init_video_upload(ALIAS, MIN_CHUNK_BYTES, MIN_CHUNK_BYTES, 1)
    first_response = await upload_video_chunk("publish-123", UPLOAD_URL, 0, chunk_b64)
    second_response = await upload_video_chunk("publish-123", UPLOAD_URL, 0, chunk_b64)

    assert init_response == {"publish_id": "publish-123", "upload_url": UPLOAD_URL}
    assert first_response["status"] == "CHUNK_UPLOADED"
    assert second_response["status"] == "CHUNK_UPLOADED"
    assert fake_client.chunk_ranges == [
        f"bytes 0-{MIN_CHUNK_BYTES - 1}/{MIN_CHUNK_BYTES}",
        f"bytes 0-{MIN_CHUNK_BYTES - 1}/{MIN_CHUNK_BYTES}",
    ]


@pytest.mark.asyncio
async def test_token_refresh_mid_upload() -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    await _store_app_credentials(backend)
    authorizations: list[str] = []
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/upload/chunk":
            authorizations.append(request.headers["authorization"])
            if len(authorizations) == 1:
                return httpx.Response(401, request=request)
            return httpx.Response(200, request=request)
        if request.url.path == OAUTH_TOKEN_PATH:
            assert "refresh_token=posting-refresh" in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-posting-access",
                    "refresh_token": "fresh-posting-refresh",
                    "expires_in": 3600,
                    "refresh_expires_in": 7200,
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    async with _client(backend, handler) as client:
        _ = await client.put_chunk_to_url(
            ALIAS,
            UPLOAD_URL,
            headers={"Content-Range": f"bytes 0-{MIN_CHUNK_BYTES - 1}/{MIN_CHUNK_BYTES}"},
            content=b"x" * MIN_CHUNK_BYTES,
        )

    assert seen_paths == ["/upload/chunk", OAUTH_TOKEN_PATH, "/upload/chunk"]
    assert authorizations == ["Bearer posting-access", "Bearer fresh-posting-access"]
    stored = await backend.get(account_key(ApiType.CONTENT_POSTING, False, ALIAS))
    assert stored is not None
    assert "fresh-posting-access" in stored
    assert "fresh-posting-refresh" in stored


@pytest.mark.asyncio
async def test_drafts_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    fake_client = FakePostingClient()
    monkeypatch.setattr(upload_tools, "_build_posting_client", lambda: fake_client)

    _ = await init_video_upload(ALIAS, MIN_CHUNK_BYTES, MIN_CHUNK_BYTES, 1)

    assert len(fake_client.request_bodies) == 1
    body = fake_client.request_bodies[0]
    assert "post_info" not in body
    assert body["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": MIN_CHUNK_BYTES,
        "chunk_size": MIN_CHUNK_BYTES,
        "total_chunk_count": 1,
    }


def test_video_upload_tool_markers_are_registered() -> None:
    assert getattr(init_video_upload, "__tiktok_mcp_destructive__", False) is True
    assert getattr(upload_video_chunk, "__tiktok_mcp_destructive__", False) is True
    assert getattr(init_video_upload, "__tiktok_mcp_write_api__", None) == "posting"


class FakePostingClient:
    def __init__(self) -> None:
        self.request_bodies: list[dict[str, object]] = []
        self.chunk_ranges: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback

    async def request(
        self,
        alias: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, object],
    ) -> dict[str, object]:
        assert alias == ALIAS
        assert method == "POST"
        assert path == INIT_VIDEO_UPLOAD_PATH
        self.request_bodies.append(json_body)
        return {"publish_id": "publish-123", "upload_url": UPLOAD_URL}

    async def put_chunk_to_url(
        self,
        alias: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes,
    ) -> httpx.Response:
        assert alias == ALIAS
        assert url == UPLOAD_URL
        assert len(content) == MIN_CHUNK_BYTES
        self.chunk_ranges.append(headers["Content-Range"])
        request = httpx.Request("PUT", url, headers=headers, content=content)
        return httpx.Response(200, request=request)


def _client(
    backend: MemoryBackend,
    handler: Callable[[httpx.Request], httpx.Response],
) -> PostingAPIClient:
    transport = httpx.MockTransport(handler)
    return PostingAPIClient(backend=backend, http_client=httpx.AsyncClient(transport=transport))


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
