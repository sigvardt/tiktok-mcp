from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tiktok_mcp.api.posting import CreatorInfo, PostPublishStatus, PostStatus
from tiktok_mcp.api.posting.client import (
    BASE_URL,
    CREATOR_INFO_PATH,
    OAUTH_TOKEN_PATH,
    POST_STATUS_PATH,
    PostingAPIClient,
)
from tiktok_mcp.auth.keychain import account_key, app_creds_key, serialize_account_record
from tiktok_mcp.observability.rate_limit_tracker import get_posture, reset_tracker
from tiktok_mcp.tools import posting_read as posting_read_tools
from tiktok_mcp.tools.posting_read import (
    posting_get_creator_info,
    posting_get_post_status,
    posting_list_drafts,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType
from tiktok_mcp.types.errors import RateLimitedError

ALIAS = "posting-alias"


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
def reset_posting_rate_limits() -> Iterator[None]:
    reset_tracker()
    yield
    reset_tracker()


@pytest.mark.asyncio
async def test_post_status_uses_bearer_auth_and_decodes_display_envelope() -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _display_response(
            request,
            {
                "status": "PUBLISH_COMPLETE",
                "uploaded_bytes": 4096,
                "video_seconds": 12,
                "publicaly_available_post_id": "public-post-1",
                "fail_reason": None,
            },
        )

    async with _client(backend, handler) as client:
        status = await client.get_post_status(ALIAS, "publish-123")

    assert status.status is PostPublishStatus.PUBLISH_COMPLETE
    assert status.uploaded_bytes == 4096
    assert status.publicaly_available_post_id == "public-post-1"
    assert len(requests) == 1
    assert str(requests[0].url) == f"{BASE_URL}{POST_STATUS_PATH}"
    assert requests[0].headers["authorization"] == "Bearer posting-access"
    assert json.loads(requests[0].content) == {"publish_id": "publish-123"}
    posture = await get_posture(ALIAS)
    assert posture[0].api_type is ApiType.CONTENT_POSTING
    assert posture[0].recent_request_count_last_60s == 1


@pytest.mark.asyncio
async def test_creator_info_is_not_cached() -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _display_response(request, _creator_info_payload(max_duration=60 * calls))

    async with _client(backend, handler) as client:
        first = await client.get_creator_info(ALIAS)
        second = await client.get_creator_info(ALIAS)

    assert first.max_video_post_duration_sec == 60
    assert second.max_video_post_duration_sec == 120
    assert calls == 2


@pytest.mark.asyncio
async def test_creator_info_accepts_live_sandbox_shape_without_interaction_flags() -> None:
    backend = MemoryBackend()
    await _store_account(backend)

    def handler(request: httpx.Request) -> httpx.Response:
        return _display_response(request, _live_sandbox_creator_info_payload())

    async with _client(backend, handler) as client:
        creator_info = await client.get_creator_info(ALIAS)

    assert creator_info.creator_nickname == "POW..."
    assert creator_info.creator_avatar_url.endswith("shcp=bbadf38d&idc=no1a")
    assert creator_info.creator_username is None
    assert creator_info.privacy_level_options is None
    assert creator_info.max_video_post_duration_sec is None
    assert creator_info.comment_disabled is None
    assert creator_info.duet_disabled is None
    assert creator_info.stitch_disabled is None
    assert "comment_disabled_supported" not in creator_info.model_dump()


@pytest.mark.asyncio
async def test_expired_token_refreshes_under_content_posting_app_credentials() -> None:
    backend = MemoryBackend()
    await _store_account(backend, access_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    await _store_app_credentials(backend)
    seen_paths: list[str] = []
    creator_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
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
        creator_authorization.append(request.headers["authorization"])
        return _display_response(request, _creator_info_payload(max_duration=180))

    async with _client(backend, handler) as client:
        creator_info = await client.get_creator_info(ALIAS)

    assert creator_info.max_video_post_duration_sec == 180
    assert seen_paths == [OAUTH_TOKEN_PATH, CREATOR_INFO_PATH]
    assert creator_authorization == ["Bearer fresh-posting-access"]
    stored = await backend.get(account_key(ApiType.CONTENT_POSTING, False, ALIAS))
    assert stored is not None
    assert "fresh-posting-access" in stored
    assert "fresh-posting-refresh" in stored


@pytest.mark.asyncio
async def test_retry_after_429_records_rate_limit_and_retries() -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return _display_response(
            request,
            {
                "status": "PROCESSING_UPLOAD",
                "uploaded_bytes": 1,
                "video_seconds": 0,
                "publicaly_available_post_id": None,
                "fail_reason": None,
            },
        )

    async with _client(backend, handler) as client:
        status = await client.get_post_status(ALIAS, "publish-456")

    assert status.status is PostPublishStatus.PROCESSING_UPLOAD
    assert calls == 2
    posture = await get_posture(ALIAS)
    assert posture[0].last_429_at is not None
    assert posture[0].last_retry_after_seconds == 0.0
    assert posture[0].recent_request_count_last_60s == 2


@pytest.mark.asyncio
async def test_terminal_429_returns_typed_rate_limited_error() -> None:
    backend = MemoryBackend()
    await _store_account(backend)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"}, request=request)

    async with _client(backend, handler) as client:
        with pytest.raises(RateLimitedError) as exc_info:
            _ = await client.get_creator_info(ALIAS)

    assert exc_info.value.retry_after == 0.0
    assert exc_info.value.attempts == 3


@pytest.mark.asyncio
async def test_post_status_rejects_unknown_status() -> None:
    backend = MemoryBackend()
    await _store_account(backend)

    def handler(request: httpx.Request) -> httpx.Response:
        return _display_response(request, {"status": "NOT_A_REAL_STATUS"})

    async with _client(backend, handler) as client:
        with pytest.raises(ValidationError):
            _ = await client.get_post_status(ALIAS, "publish-unknown")


@pytest.mark.asyncio
async def test_posting_list_drafts_reports_endpoint_gap() -> None:
    response = cast(
        dict[str, object],
        await posting_list_drafts(ALIAS, max_count=10, cursor=123),
    )

    assert response == {
        "endpoint_not_available": True,
        "reason": "TikTok has not exposed a drafts-list endpoint in v2 as of 2026-05-22",
    }


@pytest.mark.asyncio
async def test_mcp_tools_return_serializable_models(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient()
    monkeypatch.setattr(posting_read_tools, "_build_posting_client", lambda: fake_client)

    status = cast(dict[str, object], await posting_get_post_status(ALIAS, "publish-tool"))
    creator_info = cast(dict[str, object], await posting_get_creator_info(ALIAS))

    assert status["status"] == "FAILED"
    assert status["fail_reason"] == "upload_failed"
    assert creator_info["creator_username"] == "demo_creator"
    assert fake_client.seen == [("status", ALIAS, "publish-tool"), ("creator", ALIAS)]


class FakePostingClient:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str] | tuple[str, str, str]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback

    async def get_post_status(self, alias: str, publish_id: str) -> PostStatus:
        self.seen.append(("status", alias, publish_id))
        return PostStatus(status=PostPublishStatus.FAILED, fail_reason="upload_failed")

    async def get_creator_info(self, alias: str) -> CreatorInfo:
        self.seen.append(("creator", alias))
        return CreatorInfo.model_validate(_creator_info_payload(max_duration=300))


def _client(
    backend: MemoryBackend,
    handler: Callable[[httpx.Request], httpx.Response],
) -> PostingAPIClient:
    transport = httpx.MockTransport(handler)
    return PostingAPIClient(backend=backend, http_client=httpx.AsyncClient(transport=transport))


async def _store_account(
    backend: MemoryBackend,
    *,
    access_expires_at: datetime | None = None,
) -> None:
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
        access_token_expires_at=access_expires_at or datetime.now(UTC) + timedelta(hours=1),
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


def _display_response(request: httpx.Request, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": data}, request=request)


def _creator_info_payload(max_duration: int) -> dict[str, object]:
    return {
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS"],
        "max_video_post_duration_sec": max_duration,
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "creator_avatar_url": "https://example.test/avatar.png",
        "creator_username": "demo_creator",
        "creator_nickname": "Demo Creator",
    }


def _live_sandbox_creator_info_payload() -> dict[str, object]:
    return {
        "creator_nickname": "POW...",
        "creator_avatar_url": (
            "https://p16-sign-va.tiktokcdn.com/tos-maliva-avt-0068/"
            "creator.jpeg?shcp=bbadf38d&idc=no1a"
        ),
    }
