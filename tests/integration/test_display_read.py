from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false, reportAny=false
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false
import json
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

import tiktok_mcp.api.display.client as display_client_module
import tiktok_mcp.auth.keychain as keychain_module
from tiktok_mcp.api.display.client import DISPLAY_BASE_URL, DISPLAY_TOKEN_PATH, DisplayAPIClient
from tiktok_mcp.auth.keychain import (
    account_key,
    app_creds_key,
    deserialize_account_record,
    serialize_account_record,
)
from tiktok_mcp.observability.rate_limit_tracker import reset_tracker
from tiktok_mcp.tools import display_read as display_read_tools
from tiktok_mcp.tools.display_read import (
    DEFAULT_USER_FIELDS,
    DEFAULT_VIDEO_FIELDS,
    OAUTH_REVOKE_PATH,
    USER_INFO_PATH,
    VIDEO_LIST_PATH,
    VIDEO_METRICS_FIELDS,
    VIDEO_QUERY_PATH,
    display_get_user_info,
    display_get_video_metrics,
    display_list_videos,
    display_query_videos,
    display_refresh_token,
    display_revoke_token,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType

ALIAS = "display-alias"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


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


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryBackend]:
    memory_backend = MemoryBackend()
    monkeypatch.setattr(keychain_module, "_backend", memory_backend)
    display_client_module._REFRESH_LOCKS.clear()
    reset_tracker()
    yield memory_backend
    reset_tracker()
    display_client_module._REFRESH_LOCKS.clear()
    monkeypatch.setattr(keychain_module, "_backend", None)


@pytest.mark.asyncio
async def test_display_get_user_info_uses_scope_gated_default_fields(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_display_account(backend, ALIAS, scopes=["user.info.basic"])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _display_response(
            request,
            {
                "user": {
                    "open_id": "open-basic",
                    "display_name": "Basic Creator",
                    "avatar_url": "https://example.test/avatar.png",
                    "union_id": "union-basic",
                },
            },
        )

    _patch_http_client(monkeypatch, handler)

    result = await display_get_user_info(ALIAS)

    assert requests[0].method == "GET"
    assert requests[0].url.path == USER_INFO_PATH
    assert requests[0].url.params["fields"] == ",".join(DEFAULT_USER_FIELDS[:6])
    assert requests[0].headers["authorization"] == "Bearer display-access"
    assert requests[0].content == b""
    assert result["open_id"] == "open-basic"
    assert result["display_name"] == "Basic Creator"
    assert result["union_id"] == "union-basic"


@pytest.mark.asyncio
async def test_display_list_videos_preserves_pagination_passthrough(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_display_account(backend, ALIAS)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _display_response(
            request,
            {
                "videos": [_video_payload("video-1", view_count=101)],
                "cursor": 456,
                "has_more": True,
            },
        )

    _patch_http_client(monkeypatch, handler)

    result = await display_list_videos(ALIAS, cursor=123, max_count=7)

    assert requests[0].method == "POST"
    assert requests[0].url.path == VIDEO_LIST_PATH
    assert requests[0].url.params["fields"] == ",".join(DEFAULT_VIDEO_FIELDS)
    assert _json_body(requests[0]) == {
        "cursor": 123,
        "max_count": 7,
    }
    assert result["cursor"] == 456
    assert result["has_more"] is True
    assert cast(list[dict[str, object]], result["videos"])[0]["id"] == "video-1"


@pytest.mark.asyncio
async def test_display_query_videos_posts_filters_and_validates_limit(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_display_account(backend, ALIAS)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _display_response(
            request,
            {"videos": [_video_payload("video-a"), _video_payload("video-b")]},
        )

    _patch_http_client(monkeypatch, handler)

    result = await display_query_videos(ALIAS, ["video-a", "video-b"], fields=["id"])

    assert requests[0].method == "POST"
    assert requests[0].url.path == VIDEO_QUERY_PATH
    assert requests[0].url.params["fields"] == "id"
    assert _json_body(requests[0]) == {
        "filters": {"video_ids": ["video-a", "video-b"]},
    }
    assert [video["id"] for video in result] == ["video-a", "video-b"]
    with pytest.raises(ValueError, match="at most 20"):
        _ = await display_query_videos(ALIAS, [f"video-{index}" for index in range(21)])


@pytest.mark.asyncio
async def test_display_get_video_metrics_uses_metrics_field_set(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_display_account(backend, ALIAS)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _display_response(
            request,
            {"videos": [_video_payload("metric-video", view_count=999, like_count=45)]},
        )

    _patch_http_client(monkeypatch, handler)

    result = await display_get_video_metrics(ALIAS, "metric-video")

    assert requests[0].method == "POST"
    assert requests[0].url.path == VIDEO_QUERY_PATH
    assert requests[0].url.params["fields"] == ",".join(VIDEO_METRICS_FIELDS)
    assert _json_body(requests[0]) == {
        "filters": {"video_ids": ["metric-video"]},
    }
    assert result["id"] == "metric-video"
    assert result["view_count"] == 999
    assert result["like_count"] == 45


@pytest.mark.asyncio
async def test_display_refresh_token_is_gated_and_forces_refresh(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_display_account(backend, ALIAS, refresh_token="old-refresh")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = urllib.parse.parse_qs(request.content.decode())
        assert form["refresh_token"] == ["old-refresh"]
        return httpx.Response(
            200,
            json={
                "access_token": "new-display-access",
                "refresh_token": "new-display-refresh",
                "expires_in": 3600,
                "refresh_expires_in": 7200,
            },
            request=request,
        )

    _patch_http_client(monkeypatch, handler)

    blocked = await display_refresh_token(ALIAS)
    assert blocked["error"] == "writes_disabled"
    assert requests == []

    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "display")
    result = await display_refresh_token(ALIAS)

    assert requests[0].url.path == DISPLAY_TOKEN_PATH
    assert result["refreshed"] is True
    stored_account, stored_tokens = _stored_account_and_tokens(backend, ALIAS)
    assert stored_account.status is AccountStatus.OK
    assert stored_tokens.access_token.get_secret_value() == "new-display-access"
    assert stored_tokens.refresh_token is not None
    assert stored_tokens.refresh_token.get_secret_value() == "new-display-refresh"


@pytest.mark.asyncio
async def test_display_revoke_token_is_gated_and_marks_revoked_without_delete(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_display_account(backend, ALIAS)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _display_response(request, {"revoked": True})

    _patch_http_client(monkeypatch, handler)

    blocked = await display_revoke_token(ALIAS)
    assert blocked["error"] == "writes_disabled"
    assert requests == []

    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "display")
    result = await display_revoke_token(ALIAS)

    assert requests[0].url.path == OAUTH_REVOKE_PATH
    assert requests[0].headers["authorization"] == "Bearer display-access"
    assert _json_body(requests[0]) == {}
    assert result == {"alias": ALIAS, "revoked": True, "status": AccountStatus.REVOKED.value}
    stored = backend.values[account_key(ApiType.DISPLAY, False, ALIAS)]
    stored_account, stored_tokens = deserialize_account_record(stored)
    assert stored_account.status is AccountStatus.REVOKED
    assert stored_tokens.access_token.get_secret_value() == "display-access"


@pytest.mark.asyncio
async def test_multi_account_isolation_uses_distinct_bearer_tokens(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_display_account(backend, "alias-a", access_token="token-a")
    await _store_display_account(backend, "alias-b", access_token="token-b")
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["authorization"]
        authorizations.append(authorization)
        return _display_response(
            request,
            {"user": {"open_id": authorization.removeprefix("Bearer ")}},
        )

    _patch_http_client(monkeypatch, handler)

    first = await display_get_user_info("alias-a", fields=["open_id"])
    second = await display_get_user_info("alias-b", fields=["open_id"])

    assert authorizations == ["Bearer token-a", "Bearer token-b"]
    assert first["open_id"] == "token-a"
    assert second["open_id"] == "token-b"


@pytest.mark.asyncio
async def test_display_read_tools_accept_explicit_sandbox_namespace(
    backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _store_app_credentials(backend)
    await _store_app_credentials(backend, sandbox=True)
    await _store_display_account(backend, ALIAS, access_token="prod-token")
    await _store_display_account(backend, ALIAS, sandbox=True, access_token="sandbox-token")
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["authorization"]
        authorizations.append(authorization)
        return _display_response(
            request,
            {"user": {"open_id": authorization.removeprefix("Bearer ")}},
        )

    _patch_http_client(monkeypatch, handler)

    result = await display_get_user_info(ALIAS, fields=["open_id"], sandbox=True)

    assert authorizations == ["Bearer sandbox-token"]
    assert result["open_id"] == "sandbox-token"


def test_display_tool_markers_are_registered() -> None:
    assert getattr(display_get_user_info, "__tiktok_mcp_read_only__", False) is True
    assert getattr(display_list_videos, "__tiktok_mcp_read_only__", False) is True
    assert getattr(display_query_videos, "__tiktok_mcp_read_only__", False) is True
    assert getattr(display_get_video_metrics, "__tiktok_mcp_read_only__", False) is True
    assert getattr(display_refresh_token, "__tiktok_mcp_destructive__", False) is True
    assert getattr(display_revoke_token, "__tiktok_mcp_destructive__", False) is True
    assert display_read_tools.display_refresh_token is display_refresh_token


def _patch_http_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def build_http_client(self: DisplayAPIClient) -> httpx.AsyncClient:
        _ = self
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=DISPLAY_BASE_URL,
            timeout=30.0,
        )

    monkeypatch.setattr(DisplayAPIClient, "_build_http_client", build_http_client)


async def _store_app_credentials(backend: MemoryBackend, *, sandbox: bool = False) -> None:
    await backend.set(
        app_creds_key(ApiType.DISPLAY, sandbox),
        json.dumps(
            {
                "api_type": ApiType.DISPLAY.value,
                "sandbox": sandbox,
                "client_id": "display-client-id",
                "client_secret": "display-client-secret",
                "created_at": NOW.isoformat(),
            },
        ),
    )


async def _store_display_account(
    backend: MemoryBackend,
    alias: str,
    *,
    access_token: str = "display-access",
    refresh_token: str = "display-refresh",
    scopes: Sequence[str] | None = None,
    sandbox: bool = False,
) -> None:
    account = Account(
        alias=alias,
        api_type=ApiType.DISPLAY,
        sandbox=sandbox,
        tiktok_id=f"{alias}-open-id",
        display_name="Display Creator",
        avatar_url=None,
        scopes=list(
            scopes or ["user.info.basic", "user.info.profile", "user.info.stats", "video.list"],
        ),
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
    )
    tokens = AccountTokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token),
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
        last_rotated_at=datetime.now(UTC),
    )
    await backend.set(
        account_key(ApiType.DISPLAY, sandbox, alias),
        serialize_account_record(account, tokens),
    )


def _stored_account_and_tokens(
    backend: MemoryBackend,
    alias: str,
) -> tuple[Account, AccountTokens]:
    stored = backend.values[account_key(ApiType.DISPLAY, False, alias)]
    return deserialize_account_record(stored)


def _display_response(request: httpx.Request, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": data}, request=request)


def _json_body(request: httpx.Request) -> dict[str, object]:
    payload = cast(object, json.loads(request.content.decode()))
    assert isinstance(payload, dict)
    return {str(key): value for key, value in payload.items()}


def _video_payload(
    video_id: str,
    *,
    view_count: int = 100,
    like_count: int = 10,
) -> dict[str, object]:
    return {
        "id": video_id,
        "create_time": 1_779_456_000,
        "cover_image_url": "https://example.test/cover.jpg",
        "share_url": f"https://www.tiktok.com/@demo/video/{video_id}",
        "video_description": "Demo video",
        "duration": 12,
        "height": 1920,
        "width": 1080,
        "title": "Demo",
        "embed_html": "<blockquote></blockquote>",
        "embed_link": f"https://www.tiktok.com/embed/{video_id}",
        "like_count": like_count,
        "comment_count": 3,
        "share_count": 4,
        "view_count": view_count,
    }
