from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import httpx
import pytest
import vcr
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.tools import comments_read as comments_read_tools
from tiktok_mcp.tools.comments_read import (
    COMMENT_LIST_PATH,
    COMMENT_REPLY_LIST_PATH,
    comments_list,
    comments_list_replies,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "comments-demo"
BUSINESS_ID = "business-open-id"
VIDEO_ID = "video-456"
COMMENT_ID = "comment-001"
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes"
LONG_COMMENT_TEXT = (
    "This is a deliberately long raw comment body containing personal context "
    "that must never be written into cassettes or INFO logs."
)


def scrub_comment_response_body(response: dict[str, object]) -> dict[str, object]:
    body = response.get("body")
    if not isinstance(body, dict):
        return response
    raw_body = body.get("string")
    if isinstance(raw_body, bytes):
        raw_text = raw_body.decode("utf-8")
    elif isinstance(raw_body, str):
        raw_text = raw_body
    else:
        return response
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return response

    _scrub_comment_text_fields(payload)
    scrubbed_body = dict(body)
    scrubbed_body["string"] = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    scrubbed_response = dict(response)
    scrubbed_response["body"] = scrubbed_body
    return scrubbed_response


COMMENTS_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Access-Token", "REDACTED")],
    before_record_response=scrub_comment_response_body,
)


@pytest.mark.asyncio
async def test_comments_list_happy_path_with_scrubbed_cassette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return _cassette_response("comments_list.yaml", request)

    client = _client(handler)
    monkeypatch.setattr(comments_read_tools, "_build_comments_client", lambda alias: client)

    result = await comments_list(
        ALIAS,
        VIDEO_ID,
        business_id=BUSINESS_ID,
        cursor=0,
        max_count=30,
        status="ALL",
        sort_field="create_time",
        sort_order="desc",
        include_replies=True,
    )

    comments = cast(list[dict[str, object]], result["comments"])
    assert result["cursor"] == 1
    assert result["has_more"] is False
    assert result["max_count"] == 30
    assert result["total"] == 1
    assert comments[0]["comment_id"] == COMMENT_ID
    assert comments[0]["text"] == "[SCRUBBED]"
    assert comments[0]["like_count"] == 14
    assert comments[0]["reply_count"] == 2
    assert seen_requests[0].url.path == COMMENT_LIST_PATH
    assert seen_requests[0].headers["Access-Token"] == "comment-access-token"
    assert dict(seen_requests[0].url.params) == {
        "business_id": BUSINESS_ID,
        "video_id": VIDEO_ID,
        "status": "ALL",
        "cursor": "0",
        "max_count": "30",
        "sort_field": "create_time",
        "sort_order": "desc",
        "include_replies": "true",
    }


@pytest.mark.asyncio
async def test_comments_list_replies_happy_path_with_scrubbed_cassette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return _cassette_response("comments_replies.yaml", request)

    client = _client(handler)
    monkeypatch.setattr(comments_read_tools, "_build_comments_client", lambda alias: client)

    result = await comments_list_replies(
        ALIAS,
        VIDEO_ID,
        COMMENT_ID,
        business_id=BUSINESS_ID,
        cursor=2,
        max_count=10,
        status="PUBLIC",
        sort_field="create_time",
        sort_order="asc",
    )

    comments = cast(list[dict[str, object]], result["comments"])
    assert result["cursor"] == 3
    assert result["has_more"] is False
    assert result["max_count"] == 10
    assert result["total"] == 1
    assert comments[0]["parent_comment_id"] == COMMENT_ID
    assert comments[0]["text"] == "[SCRUBBED]"
    assert seen_requests[0].url.path == COMMENT_REPLY_LIST_PATH
    assert dict(seen_requests[0].url.params) == {
        "business_id": BUSINESS_ID,
        "video_id": VIDEO_ID,
        "comment_id": COMMENT_ID,
        "status": "PUBLIC",
        "cursor": "2",
        "max_count": "10",
        "sort_field": "create_time",
        "sort_order": "asc",
    }


@pytest.mark.asyncio
async def test_comments_list_uses_stored_business_id_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeCommentsClient(_raw_comment_payload(text="[SCRUBBED]"))
    monkeypatch.setattr(comments_read_tools, "_build_comments_client", lambda alias: fake_client)

    async def business_id_for_alias(alias: str) -> str:
        assert alias == ALIAS
        return BUSINESS_ID

    monkeypatch.setattr(comments_read_tools, "_business_id_for_alias", business_id_for_alias)

    _ = await comments_list(ALIAS, VIDEO_ID)

    _path, params = fake_client.requests[0]
    assert params is not None
    assert params["business_id"] == BUSINESS_ID
    assert params["video_id"] == VIDEO_ID


@pytest.mark.asyncio
async def test_comment_text_is_not_logged_at_info(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeCommentsClient(_raw_comment_payload(text=LONG_COMMENT_TEXT))
    monkeypatch.setattr(comments_read_tools, "_build_comments_client", lambda alias: fake_client)

    with caplog.at_level(logging.INFO, logger="tiktok_mcp.tools.comments_read"):
        result = await comments_list(ALIAS, VIDEO_ID, business_id=BUSINESS_ID)

    comments = cast(list[dict[str, object]], result["comments"])
    assert comments[0]["text"] == LONG_COMMENT_TEXT
    assert "raw-comment-001" in caplog.text
    assert LONG_COMMENT_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_debug_comment_body_logging_requires_env_opt_in(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeCommentsClient(_raw_comment_payload(text=LONG_COMMENT_TEXT))
    monkeypatch.setattr(comments_read_tools, "_build_comments_client", lambda alias: fake_client)
    monkeypatch.delenv("TIKTOK_MCP_LOG_COMMENT_BODIES", raising=False)

    with caplog.at_level(logging.DEBUG, logger="tiktok_mcp.tools.comments_read"):
        _ = await comments_list(ALIAS, VIDEO_ID, business_id=BUSINESS_ID)
    assert LONG_COMMENT_TEXT not in caplog.text

    caplog.clear()
    monkeypatch.setenv("TIKTOK_MCP_LOG_COMMENT_BODIES", "1")
    with caplog.at_level(logging.DEBUG, logger="tiktok_mcp.tools.comments_read"):
        _ = await comments_list(ALIAS, VIDEO_ID, business_id=BUSINESS_ID)
    assert LONG_COMMENT_TEXT in caplog.text


def test_comment_cassette_body_scrubber_removes_comment_text() -> None:
    response: dict[str, object] = {
        "body": {
            "string": json.dumps(
                {
                    "code": 0,
                    "data": {
                        "comments": [
                            {
                                "text": LONG_COMMENT_TEXT,
                                "comment_text": LONG_COMMENT_TEXT,
                            }
                        ]
                    },
                    "message": "OK",
                }
            )
        }
    }

    scrubbed = scrub_comment_response_body(response)
    body = cast(dict[str, object], scrubbed["body"])
    raw_body = cast(str, body["string"])

    assert COMMENTS_VCR is not None
    assert LONG_COMMENT_TEXT not in raw_body
    assert raw_body.count("[SCRUBBED]") == 2


def test_authored_comment_cassettes_keep_comment_text_scrubbed() -> None:
    for cassette_path in CASSETTE_DIR.glob("comments_*.yaml"):
        cassette_text = cassette_path.read_text(encoding="utf-8")
        assert LONG_COMMENT_TEXT not in cassette_text
        assert re.search(r"text:.{50,}", cassette_text) is None


class FakeCommentsClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[tuple[str, Mapping[str, object] | None]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        self.requests.append((path, params))
        return self.payload


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> BusinessAPIClient:
    return BusinessAPIClient(_account(), _credentials(), transport=httpx.MockTransport(handler))


def _account() -> AccountWithTokens:
    now = datetime.now(UTC)
    return AccountWithTokens(
        alias=ALIAS,
        api_type=ApiType.BUSINESS_ORGANIC,
        sandbox=True,
        tiktok_id="business-open-id",
        display_name="Comments Demo",
        avatar_url=None,
        scopes=["business.comment.management"],
        created_at=now,
        last_used_at=None,
        status=AccountStatus.OK,
        access_token=SecretStr("comment-access-token"),
        refresh_token=SecretStr("comment-refresh-token"),
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_expires_at=now + timedelta(days=30),
        last_rotated_at=now,
    )


def _credentials() -> AppCredentials:
    return AppCredentials(
        api_type=ApiType.BUSINESS_ORGANIC,
        sandbox=True,
        client_id=SecretStr("comments-client-id"),
        client_secret=SecretStr("comments-client-secret"),
        created_at=datetime.now(UTC),
    )


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


def _raw_comment_payload(text: str) -> dict[str, Any]:
    return {
        "comments": [
            {
                "comment_id": "raw-comment-001",
                "author": {
                    "open_id": "open-raw-001",
                    "display_name": "Raw Commenter",
                    "avatar_url": "https://example.test/raw.png",
                },
                "text": text,
                "like_count": 3,
                "reply_count": 1,
                "create_time": 1_716_379_200,
                "is_top_pinned": False,
                "is_hidden_by_owner": False,
                "is_deleted_by_author": False,
            }
        ],
        "page": 1,
        "max_count": 30,
        "cursor": 1,
        "has_more": False,
        "total": 1,
    }


def _scrub_comment_text_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "comment_text"} and isinstance(item, str):
                value[key] = "[SCRUBBED]"
            else:
                _scrub_comment_text_fields(item)
    elif isinstance(value, list):
        for item in value:
            _scrub_comment_text_fields(item)
