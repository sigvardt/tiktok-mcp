from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
from types import TracebackType
from typing import Self

import pytest
from pydantic import ValidationError

from tiktok_mcp.tools import posting_writes_drafts as drafts_tools
from tiktok_mcp.tools.posting_writes_drafts import (
    DRAFT_DELETE_PATH,
    DRAFT_PUBLISH_PATH,
    delete_draft,
    list_pending_drafts,
    move_draft_to_publish,
)

ALIAS = "posting-alias"


@pytest.mark.asyncio
async def test_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient()
    monkeypatch.setattr(drafts_tools, "_build_posting_client", lambda: fake_client)

    publish_result = await move_draft_to_publish(
        "draft-fixture-1",
        {"title": "QA test", "privacy_level": "SELF_ONLY"},
    )
    delete_result = await delete_draft("draft-fixture-1")

    assert publish_result["error"] == "writes_disabled"
    assert publish_result["api"] == "posting"
    assert delete_result["error"] == "writes_disabled"
    assert fake_client.requests == []


@pytest.mark.asyncio
async def test_allowed_publish_posts_validated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient({"status": "PROCESSING_UPLOAD"})
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setattr(drafts_tools, "_build_posting_client", lambda: fake_client)
    monkeypatch.setattr(drafts_tools, "_single_content_posting_alias", _fake_alias)

    result = await move_draft_to_publish(
        "draft-fixture-1",
        {
            "title": "QA test",
            "privacy_level": "SELF_ONLY",
            "disable_comment": True,
            "auto_add_music": False,
        },
    )

    assert result == {"publish_id": "draft-fixture-1", "status": "PROCESSING_UPLOAD"}
    assert fake_client.requests == [
        (
            ALIAS,
            "POST",
            DRAFT_PUBLISH_PATH,
            {
                "publish_id": "draft-fixture-1",
                "post_info": {
                    "title": "QA test",
                    "privacy_level": "SELF_ONLY",
                    "disable_comment": True,
                    "auto_add_music": False,
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_delete_draft_posts_publish_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient({"deleted": True})
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setattr(drafts_tools, "_build_posting_client", lambda: fake_client)
    monkeypatch.setattr(drafts_tools, "_single_content_posting_alias", _fake_alias)

    result = await delete_draft("draft-fixture-1")

    assert result == {"publish_id": "draft-fixture-1", "deleted": True}
    assert fake_client.requests == [
        (ALIAS, "POST", DRAFT_DELETE_PATH, {"publish_id": "draft-fixture-1"})
    ]


@pytest.mark.asyncio
async def test_list_drafts_not_gated() -> None:
    response = await list_pending_drafts(ALIAS)

    assert response["endpoint_not_available"] is True
    assert response["alias"] == ALIAS
    assert "writes_disabled" not in response


@pytest.mark.asyncio
async def test_publish_requires_privacy_level(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakePostingClient()
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setattr(drafts_tools, "_build_posting_client", lambda: fake_client)
    monkeypatch.setattr(drafts_tools, "_single_content_posting_alias", _forbidden_alias)

    with pytest.raises(ValidationError, match="privacy_level"):
        _ = await move_draft_to_publish("draft-fixture-1", {"title": "QA test"})

    assert fake_client.requests == []


def test_draft_tool_markers_are_registered() -> None:
    assert getattr(move_draft_to_publish, "__tiktok_mcp_destructive__", False) is True
    assert getattr(delete_draft, "__tiktok_mcp_destructive__", False) is True
    assert getattr(list_pending_drafts, "__tiktok_mcp_read_only__", False) is True


async def _fake_alias() -> str:
    return ALIAS


async def _forbidden_alias() -> str:
    raise AssertionError("alias resolution must not happen after validation failure")


class FakePostingClient:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload: dict[str, object] = payload or {"ok": True}
        self.requests: list[tuple[str, str, str, dict[str, object]]] = []

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
        self.requests.append((alias, method, path, json_body))
        return self.payload
