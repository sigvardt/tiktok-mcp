# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import httpx
import pytest
import vcr  # type: ignore[import-untyped]
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.tools import comments_writes as comments_write_tools
from tiktok_mcp.tools.comments_writes import (
    COMMENT_HIDE_PATH,
    COMMENT_PIN_PATH,
    COMMENT_REPLY_CREATE_PATH,
    COMMENT_REPLY_DELETE_PATH,
    delete_own_reply,
    hide_comment,
    pin_comment,
    post_comment_reply,
    unhide_comment,
    unpin_comment,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "comments-demo"
BUSINESS_ID = "business-123"
ACCOUNT_ID = "account-456"
COMMENT_ID = "comment-001"
REPLY_ID = "reply-001"
REPLY_TEXT = "Sensitive customer reply body"
LONG_COMMENT_TEXT = (
    "This raw comment or reply body is long enough to prove cassette PII scrubbing works."
)
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes" / "comments_writes"


class YamlModule(Protocol):
    def safe_load(self, stream: str) -> object: ...


class RecordedRequestLike(Protocol):
    body: str | bytes | None


def scrub_comment_recorded_response(response: dict[str, object]) -> dict[str, object]:
    body = response.get("body")
    if not isinstance(body, Mapping):
        return response
    body_mapping = {str(key): value for key, value in body.items()}
    scrubbed_body = _scrubbed_json_body(body_mapping.get("string"))
    if scrubbed_body is None:
        return response
    next_body = dict(body_mapping)
    next_body["string"] = scrubbed_body
    next_response = dict(response)
    next_response["body"] = next_body
    return next_response


def scrub_comment_recorded_request(request: RecordedRequestLike) -> RecordedRequestLike:
    scrubbed_body = _scrubbed_json_body(request.body)
    if scrubbed_body is not None:
        request.body = scrubbed_body
    return request


COMMENTS_WRITES_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Access-Token", "REDACTED")],
    before_record_request=scrub_comment_recorded_request,
    before_record_response=scrub_comment_recorded_response,
)


@pytest.mark.asyncio
async def test_comments_write_replay_covers_all_six_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "comments")

    reply_requests = _install_client(monkeypatch, "reply_create.yaml")
    reply = await post_comment_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID, REPLY_TEXT)

    assert reply["replied"] is True
    assert reply["reply_id"] == REPLY_ID
    assert reply["request_id"] == "req-comment-reply-create"
    assert reply_requests[0].url.path == COMMENT_REPLY_CREATE_PATH
    assert _json_body(reply_requests[0]) == {
        "business_id": BUSINESS_ID,
        "account_id": ACCOUNT_ID,
        "comment_id": COMMENT_ID,
        "comment_text": REPLY_TEXT,
    }

    pin_requests = _install_client(monkeypatch, "pin_unpin.yaml")
    pinned = await pin_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)
    unpinned = await unpin_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)

    assert pinned["action"] == "PIN"
    assert unpinned["action"] == "UNPIN"
    assert [request.url.path for request in pin_requests] == [COMMENT_PIN_PATH, COMMENT_PIN_PATH]
    assert _json_body(pin_requests[0])["action"] == "PIN"
    assert _json_body(pin_requests[1])["action"] == "UNPIN"

    hide_requests = _install_client(monkeypatch, "hide_unhide.yaml")
    hidden = await hide_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)
    unhidden = await unhide_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)

    assert hidden["action"] == "HIDE"
    assert unhidden["action"] == "UNHIDE"
    assert [request.url.path for request in hide_requests] == [COMMENT_HIDE_PATH, COMMENT_HIDE_PATH]
    assert _json_body(hide_requests[0])["action"] == "HIDE"
    assert _json_body(hide_requests[1])["action"] == "UNHIDE"

    delete_requests = _install_client(monkeypatch, "delete_own_reply.yaml")
    deleted = await delete_own_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, REPLY_ID)

    assert deleted["deleted"] is True
    assert deleted["comment_id"] == REPLY_ID
    assert deleted["request_id"] == "req-comment-reply-delete"
    assert delete_requests[0].url.path == COMMENT_REPLY_DELETE_PATH
    assert _json_body(delete_requests[0]) == {
        "business_id": BUSINESS_ID,
        "account_id": ACCOUNT_ID,
        "comment_id": REPLY_ID,
    }


def test_comments_write_response_scrubber_removes_text_fields() -> None:
    response: dict[str, object] = {
        "body": {
            "string": json.dumps(
                {
                    "code": 0,
                    "data": {
                        "text": LONG_COMMENT_TEXT,
                        "nested": {"comment_text": LONG_COMMENT_TEXT},
                    },
                    "message": "OK",
                }
            )
        }
    }

    scrubbed = scrub_comment_recorded_response(response)
    body = cast(dict[str, object], scrubbed["body"])
    raw_body = cast(str, body["string"])

    assert COMMENTS_WRITES_VCR is not None
    assert LONG_COMMENT_TEXT not in raw_body
    assert raw_body.count("[SCRUBBED]") == 2


def test_comments_write_request_scrubber_removes_reply_text() -> None:
    request = RecordedRequest(
        json.dumps(
            {
                "business_id": BUSINESS_ID,
                "account_id": ACCOUNT_ID,
                "comment_id": COMMENT_ID,
                "comment_text": LONG_COMMENT_TEXT,
            }
        )
    )

    scrubbed = scrub_comment_recorded_request(request)

    scrubbed_body = cast(str, scrubbed.body)
    assert LONG_COMMENT_TEXT not in scrubbed_body
    assert "[SCRUBBED]" in scrubbed_body


def test_authored_comment_write_cassettes_are_scrubbed() -> None:
    for cassette_path in CASSETTE_DIR.glob("*.yaml"):
        cassette_text = cassette_path.read_text(encoding="utf-8")
        assert REPLY_TEXT not in cassette_text
        assert LONG_COMMENT_TEXT not in cassette_text
        assert "Sensitive customer" not in cassette_text


class RecordedRequest:
    def __init__(self, body: str) -> None:
        self.body: str | bytes | None = body


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    cassette_name: str,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    interactions = _cassette_interactions(cassette_name)
    response_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_index
        requests.append(request)
        interaction = interactions[response_index]
        response_index += 1
        return _cassette_response(interaction, request)

    def build_client(alias: str) -> BusinessAPIClient:
        assert alias == ALIAS
        return BusinessAPIClient(
            _account(),
            _credentials(),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(comments_write_tools, "_build_comments_write_client", build_client)
    return requests


def _cassette_interactions(cassette_name: str) -> list[dict[str, object]]:
    yaml = cast(YamlModule, pytest.importorskip("yaml"))
    payload = yaml.safe_load((CASSETTE_DIR / cassette_name).read_text(encoding="utf-8"))
    cassette = cast(dict[str, object], payload)
    return cast(list[dict[str, object]], cassette["interactions"])


def _cassette_response(interaction: Mapping[str, object], request: httpx.Request) -> httpx.Response:
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


def _json_body(request: httpx.Request) -> dict[str, object]:
    payload = cast(object, json.loads(request.content.decode("utf-8")))
    if not isinstance(payload, dict):
        raise TypeError("request body must be a JSON object")
    return {str(key): item for key, item in payload.items()}


def _single_value_headers(raw_headers: Mapping[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, list) and value:
            headers[str(key)] = str(value[0])
        elif value is not None:
            headers[str(key)] = str(value)
    return headers


def _scrubbed_json_body(raw_body: object) -> str | bytes | None:
    if isinstance(raw_body, bytes):
        raw_text = raw_body.decode("utf-8")
        as_bytes = True
    elif isinstance(raw_body, str):
        raw_text = raw_body
        as_bytes = False
    else:
        return None
    try:
        payload = cast(object, json.loads(raw_text))
    except json.JSONDecodeError:
        return None
    _scrub_comment_text_fields(payload)
    scrubbed_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return scrubbed_text.encode("utf-8") if as_bytes else scrubbed_text


def _scrub_comment_text_fields(value: object) -> None:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        for key, item in mapping.items():
            if key in {"text", "comment_text"} and isinstance(item, str):
                mapping[key] = "[SCRUBBED]"
            else:
                _scrub_comment_text_fields(item)
    elif isinstance(value, list):
        for item in value:
            _scrub_comment_text_fields(item)


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
