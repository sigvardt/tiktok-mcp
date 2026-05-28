from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false, reportAttributeAccessIssue=false, reportPrivateUsage=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
from types import TracebackType
from typing import cast

import pytest

from tiktok_mcp.api.posting.client import POST_STATUS_PATH
from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.tools import posting_writes_pull_and_photo as posting_tools
from tiktok_mcp.tools.posting_writes_pull_and_photo import (
    CANCEL_PUBLISH_PATH,
    DIRECT_VIDEO_INIT_PATH,
    INBOX_VIDEO_INIT_PATH,
    PHOTO_INIT_PATH,
    cancel_publish,
    get_publish_status,
    upload_photo_from_urls,
    upload_video_from_url,
)

ALIAS = "posting-alias"
VIDEO_URL = "https://example.com/sample.mp4"
UNKNOWN_PUBLISH_ID_OR_EXPIRED_MESSAGE = (
    "TikTok returned HTTP 400 for this publish_id. The id may be unknown, expired, "
    "or malformed. Verify the publish_id from a prior upload tool."
)
PHOTO_URLS = [
    "https://example.com/photo-1.webp",
    "https://example.com/photo-2.webp",
    "https://example.com/photo-3.webp",
]


@pytest.fixture(autouse=True)
def clear_publish_aliases() -> None:
    posting_tools._PUBLISH_ALIASES.clear()


@pytest.mark.asyncio
async def test_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient()
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)

    result = cast(dict[str, object], await upload_video_from_url(ALIAS, VIDEO_URL))

    assert result["error"] == "writes_disabled"
    assert result["api"] == "posting"
    assert fake_client.requests == []


@pytest.mark.asyncio
async def test_direct_post_requires_post_info(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient()
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)

    result = cast(
        dict[str, object],
        await upload_video_from_url(
            ALIAS,
            VIDEO_URL,
            publish_immediately=True,
            post_info={"title": "Missing privacy"},
        ),
    )

    assert result["error"] == "validation_error"
    assert "privacy_level" in str(result["message"])
    assert fake_client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    ["http://example.com/video.mp4", "file:///tmp/video.mp4", "data:text/plain,x"],
)
async def test_https_only(monkeypatch: pytest.MonkeyPatch, bad_url: str) -> None:
    fake_client = FakePostingClient()
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)

    video_result = cast(dict[str, object], await upload_video_from_url(ALIAS, bad_url))
    photo_result = cast(dict[str, object], await upload_photo_from_urls(ALIAS, [bad_url]))

    assert video_result["error"] == "validation_error"
    assert photo_result["error"] == "validation_error"
    assert fake_client.requests == []


@pytest.mark.asyncio
async def test_privacy_level_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient()
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)

    result = cast(
        dict[str, object],
        await upload_video_from_url(
            ALIAS,
            VIDEO_URL,
            publish_immediately=True,
            post_info={"title": "Bad privacy", "privacy_level": "FRIENDS_ONLY"},
        ),
    )

    assert result["error"] == "validation_error"
    assert "privacy_level" in str(result["message"])
    assert fake_client.requests == []


@pytest.mark.asyncio
async def test_draft_video_routes_to_inbox_without_post_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakePostingClient(publish_id="publish-draft")
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)

    result = cast(
        dict[str, object],
        await upload_video_from_url(
            ALIAS,
            VIDEO_URL,
            post_info={"title": "Ignored draft title", "privacy_level": "SELF_ONLY"},
        ),
    )

    assert result["publish_id"] == "publish-draft"
    assert fake_client.requests[0][2] == INBOX_VIDEO_INIT_PATH
    body = fake_client.requests[0][3]
    source_info = cast(dict[str, object], body["source_info"])
    assert source_info == {"source": "PULL_FROM_URL", "video_url": VIDEO_URL}
    assert "post_info" not in body


@pytest.mark.asyncio
async def test_direct_video_routes_to_direct_post_with_post_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakePostingClient(publish_id="publish-direct")
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)

    result = cast(
        dict[str, object],
        await upload_video_from_url(
            ALIAS,
            VIDEO_URL,
            publish_immediately=True,
            post_info={"title": "Direct", "privacy_level": "SELF_ONLY"},
        ),
    )

    assert result["publish_id"] == "publish-direct"
    assert fake_client.requests[0][2] == DIRECT_VIDEO_INIT_PATH
    body = fake_client.requests[0][3]
    post_info = cast(dict[str, object], body["post_info"])
    assert post_info["title"] == "Direct"
    assert post_info["privacy_level"] == "SELF_ONLY"


@pytest.mark.asyncio
async def test_photo_urls_build_nested_image_url_array(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient(publish_id="photo-publish")
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)

    result = cast(dict[str, object], await upload_photo_from_urls(ALIAS, PHOTO_URLS))

    assert result["publish_id"] == "photo-publish"
    assert fake_client.requests[0][2] == PHOTO_INIT_PATH
    body = fake_client.requests[0][3]
    assert body["media_type"] == "PHOTO"
    assert body["post_mode"] == "MEDIA_UPLOAD"
    source_info = cast(dict[str, object], body["source_info"])
    photo_images = cast(dict[str, object], source_info["photo_images"])
    assert photo_images["image_urls"] == PHOTO_URLS


@pytest.mark.asyncio
async def test_get_publish_status_uses_cached_alias_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient(status="FETCH_IN_PROGRESS")
    monkeypatch.delenv("TIKTOK_MCP_ALLOW_WRITES", raising=False)
    monkeypatch.delenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", raising=False)
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)
    await posting_tools._remember_publish_alias("publish-123", ALIAS)

    result = cast(dict[str, object], await get_publish_status("publish-123"))

    assert result["status"] == "FETCH_IN_PROGRESS"
    assert [request[2] for request in fake_client.requests] == [POST_STATUS_PATH]


@pytest.mark.asyncio
async def test_get_publish_status_returns_unknown_publish_id_envelope_on_status_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_id = "publish-expired"
    fake_client = FakePostingClient(
        publish_id=publish_id,
        status_error=SanitizedHttpxError(
            status=400,
            url_path=POST_STATUS_PATH,
            request_id="req-400",
        ),
    )
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)
    init_result = cast(dict[str, object], await upload_video_from_url(ALIAS, VIDEO_URL))
    assert init_result["publish_id"] == publish_id

    result = cast(dict[str, object], await get_publish_status(publish_id))

    assert result == {
        "error": "unknown_publish_id_or_expired",
        "tool": "get_publish_status",
        "publish_id": publish_id,
        "message": UNKNOWN_PUBLISH_ID_OR_EXPIRED_MESSAGE,
        "request_id": "req-400",
    }
    assert fake_client.requests == [
        (
            ALIAS,
            "POST",
            INBOX_VIDEO_INIT_PATH,
            {"source_info": {"source": "PULL_FROM_URL", "video_url": VIDEO_URL}},
        ),
        (ALIAS, "POST", POST_STATUS_PATH, {"publish_id": publish_id}),
    ]


@pytest.mark.asyncio
async def test_get_publish_status_raises_non_400_status_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_id = "publish-server-error"
    fake_client = FakePostingClient(
        publish_id=publish_id,
        status_error=SanitizedHttpxError(
            status=500,
            url_path=POST_STATUS_PATH,
            request_id="req-500",
        ),
    )
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)
    init_result = cast(dict[str, object], await upload_video_from_url(ALIAS, VIDEO_URL))
    assert init_result["publish_id"] == publish_id

    with pytest.raises(SanitizedHttpxError) as exc_info:
        await get_publish_status(publish_id)

    assert exc_info.value.status == 500
    assert exc_info.value.url_path == POST_STATUS_PATH
    assert exc_info.value.request_id == "req-500"


@pytest.mark.asyncio
async def test_cancel_publish_skips_cancel_when_already_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakePostingClient(status="PUBLISH_COMPLETE")
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setattr(posting_tools, "_build_posting_client", lambda: fake_client)
    await posting_tools._remember_publish_alias("publish-terminal", ALIAS)

    result = cast(dict[str, object], await cancel_publish("publish-terminal"))

    assert result["cancelled"] is False
    assert result["already_terminal"] is True
    assert [request[2] for request in fake_client.requests] == [POST_STATUS_PATH]


def test_tool_markers_are_destructive() -> None:
    tools = [upload_video_from_url, upload_photo_from_urls, cancel_publish]
    assert all(getattr(tool, "__tiktok_mcp_destructive__", False) for tool in tools)
    assert all(getattr(tool, "__tiktok_mcp_write_api__", None) == "posting" for tool in tools)
    assert getattr(get_publish_status, "__tiktok_mcp_read_only__", False) is True
    assert getattr(get_publish_status, "__tiktok_mcp_destructive__", False) is False
    assert getattr(get_publish_status, "__tiktok_mcp_write_api__", None) is None


class FakePostingClient:
    def __init__(
        self,
        *,
        publish_id: str = "publish-123",
        status: str = "PROCESSING_UPLOAD",
        status_error: SanitizedHttpxError | None = None,
    ) -> None:
        self.publish_id: str = publish_id
        self.status: str = status
        self.status_error: SanitizedHttpxError | None = status_error
        self.requests: list[tuple[str, str, str, dict[str, object]]] = []

    async def __aenter__(self) -> FakePostingClient:
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
        self.requests.append((alias, method, path, json_body))
        if path == POST_STATUS_PATH:
            if self.status_error is not None:
                raise self.status_error
            return {"publish_id": json_body["publish_id"], "status": self.status}
        if path == CANCEL_PUBLISH_PATH:
            return {"cancelled": True}
        return {"publish_id": self.publish_id}
