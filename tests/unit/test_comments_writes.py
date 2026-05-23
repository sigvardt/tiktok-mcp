# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
from __future__ import annotations

import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Self

import pytest

from tiktok_mcp.tools import comments_writes as comments_write_tools
from tiktok_mcp.tools.comments_writes import (
    COMMENT_HIDE_PATH,
    COMMENT_PIN_PATH,
    COMMENT_REPLY_CREATE_PATH,
    COMMENT_REPLY_DELETE_PATH,
    REPLY_TEXT_MAX_LENGTH,
    delete_own_reply,
    hide_comment,
    pin_comment,
    post_comment_reply,
    unhide_comment,
    unpin_comment,
)

ALIAS = "comments-demo"
BUSINESS_ID = "business-123"
ACCOUNT_ID = "account-456"
COMMENT_ID = "comment-001"
REPLY_TEXT = "Sensitive customer reply body"


@pytest.mark.asyncio
async def test_blocked_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKTOK_MCP_ALLOW_WRITES", raising=False)
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    build_calls: list[str] = []

    def forbidden_build(alias: str) -> FakeCommentWriteClient:
        build_calls.append(alias)
        raise AssertionError("client must not be built while comments writes are blocked")

    monkeypatch.setattr(comments_write_tools, "_build_comments_write_client", forbidden_build)

    result = await hide_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)

    assert result["error"] == "writes_disabled"
    assert result["api"] == "comments"
    assert result["tool"] == "hide_comment"
    assert build_calls == []


@pytest.mark.asyncio
async def test_blocked_when_only_marketing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    build_calls: list[str] = []

    def forbidden_build(alias: str) -> FakeCommentWriteClient:
        build_calls.append(alias)
        raise AssertionError("client must not be built for non-comments write gates")

    monkeypatch.setattr(comments_write_tools, "_build_comments_write_client", forbidden_build)

    result = await pin_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)

    assert result["error"] == "writes_disabled"
    assert result["api"] == "comments"
    assert build_calls == []


@pytest.mark.asyncio
async def test_allowed_with_comments_or_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "comments")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    comments_client = _install_client(monkeypatch, request_id="req-hide")

    hidden = await hide_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)

    assert hidden["action"] == "HIDE"
    assert hidden["request_id"] == "req-hide"
    assert comments_client.requests == [
        (
            COMMENT_HIDE_PATH,
            {
                "business_id": BUSINESS_ID,
                "account_id": ACCOUNT_ID,
                "comment_id": COMMENT_ID,
                "action": "HIDE",
            },
        )
    ]

    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "all")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    all_client = _install_client(monkeypatch, request_id="req-unhide")

    unhidden = await unhide_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)

    assert unhidden["action"] == "UNHIDE"
    assert unhidden["request_id"] == "req-unhide"
    assert all_client.requests[0][0] == COMMENT_HIDE_PATH
    assert all_client.requests[0][1]["action"] == "UNHIDE"


@pytest.mark.asyncio
async def test_reply_text_not_in_log(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "comments")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.delenv("TIKTOK_MCP_LOG_COMMENT_BODIES", raising=False)
    _ = _install_client(monkeypatch, payload={"request_id": "req-reply", "reply_id": "reply-001"})

    with caplog.at_level(logging.INFO, logger="tiktok_mcp.tools.comments_writes"):
        result = await post_comment_reply(
            ALIAS,
            BUSINESS_ID,
            ACCOUNT_ID,
            COMMENT_ID,
            REPLY_TEXT,
        )

    assert result["reply_id"] == "reply-001"
    assert REPLY_TEXT not in caplog.text
    assert all(
        REPLY_TEXT not in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.INFO
    )
    action_record = caplog.records[-1]
    action_fields = action_record.__dict__
    assert action_fields["action"] == "REPLY"
    assert action_fields["business_id"] == BUSINESS_ID
    assert action_fields["account_id"] == ACCOUNT_ID
    assert action_fields["comment_id"] == COMMENT_ID
    assert action_fields["request_id"] == "req-reply"

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="tiktok_mcp.tools.comments_writes"):
        _ = await post_comment_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID, REPLY_TEXT)
    assert REPLY_TEXT not in caplog.text

    caplog.clear()
    monkeypatch.setenv("TIKTOK_MCP_LOG_COMMENT_BODIES", "1")
    with caplog.at_level(logging.DEBUG, logger="tiktok_mcp.tools.comments_writes"):
        _ = await post_comment_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID, REPLY_TEXT)
    assert REPLY_TEXT in caplog.text


@pytest.mark.asyncio
async def test_reply_max_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "comments")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    build_calls: list[str] = []

    def forbidden_build(alias: str) -> FakeCommentWriteClient:
        build_calls.append(alias)
        raise AssertionError("client must not be built after validation failure")

    monkeypatch.setattr(comments_write_tools, "_build_comments_write_client", forbidden_build)

    with pytest.raises(ValueError, match="at most 150"):
        _ = await post_comment_reply(
            ALIAS,
            BUSINESS_ID,
            ACCOUNT_ID,
            COMMENT_ID,
            "a" * (REPLY_TEXT_MAX_LENGTH + 1),
        )
    assert build_calls == []

    client = _install_client(
        monkeypatch,
        payload={"request_id": "req-reply", "reply_id": "reply-001"},
    )
    accepted_text = "a" * REPLY_TEXT_MAX_LENGTH

    result = await post_comment_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID, accepted_text)

    assert result["replied"] is True
    assert client.requests[0][0] == COMMENT_REPLY_CREATE_PATH
    assert client.requests[0][1]["comment_text"] == accepted_text


@pytest.mark.asyncio
async def test_reply_text_normalizes_nfc_and_rejects_surrogates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "comments")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    client = _install_client(
        monkeypatch,
        payload={"request_id": "req-reply", "reply_id": "reply-001"},
    )

    _ = await post_comment_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID, "e\u0301")

    assert client.requests[0][1]["comment_text"] == "é"

    with pytest.raises(ValueError, match="surrogate"):
        _ = await post_comment_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID, "\ud800")
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_pin_unpin_hide_unhide_delete_request_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "comments")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    client = _install_client(monkeypatch, request_id="req-action")

    pinned = await pin_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)
    unpinned = await unpin_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)
    hidden = await hide_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)
    unhidden = await unhide_comment(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)
    deleted = await delete_own_reply(ALIAS, BUSINESS_ID, ACCOUNT_ID, COMMENT_ID)

    assert pinned["action"] == "PIN"
    assert unpinned["action"] == "UNPIN"
    assert hidden["action"] == "HIDE"
    assert unhidden["action"] == "UNHIDE"
    assert deleted["deleted"] is True
    assert deleted["comment_id"] == COMMENT_ID
    assert isinstance(deleted["deleted_at"], str)
    assert client.requests == [
        (
            COMMENT_PIN_PATH,
            _expected_body("PIN"),
        ),
        (
            COMMENT_PIN_PATH,
            _expected_body("UNPIN"),
        ),
        (
            COMMENT_HIDE_PATH,
            _expected_body("HIDE"),
        ),
        (
            COMMENT_HIDE_PATH,
            _expected_body("UNHIDE"),
        ),
        (
            COMMENT_REPLY_DELETE_PATH,
            {
                "business_id": BUSINESS_ID,
                "account_id": ACCOUNT_ID,
                "comment_id": COMMENT_ID,
            },
        ),
    ]


def test_all_comment_write_tools_are_destructive_and_comments_gated() -> None:
    for tool in (
        post_comment_reply,
        pin_comment,
        unpin_comment,
        hide_comment,
        unhide_comment,
        delete_own_reply,
    ):
        assert getattr(tool, "__tiktok_mcp_destructive__", False) is True
        assert getattr(tool, "__tiktok_mcp_write_api__", None) == "comments"


class FakeCommentWriteClient:
    def __init__(self, payload: Mapping[str, object] | None = None) -> None:
        self.payload: dict[str, object] = dict(payload or {"request_id": "req-unit"})
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, traceback)

    async def post(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        json: object | None = None,
        idempotent: bool = False,
    ) -> dict[str, object]:
        _ = (params, idempotent)
        self.requests.append((path, _json_mapping(json)))
        return self.payload


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: Mapping[str, object] | None = None,
    request_id: str = "req-unit",
) -> FakeCommentWriteClient:
    client = FakeCommentWriteClient(payload or {"request_id": request_id})
    monkeypatch.setattr(comments_write_tools, "_build_comments_write_client", lambda alias: client)
    return client


def _json_mapping(value: object | None) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("comment write JSON body must be a mapping")
    return {str(key): item for key, item in value.items()}


def _expected_body(action: str) -> dict[str, object]:
    return {
        "business_id": BUSINESS_ID,
        "account_id": ACCOUNT_ID,
        "comment_id": COMMENT_ID,
        "action": action,
    }
