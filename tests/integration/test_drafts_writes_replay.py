from __future__ import annotations

# pyright: reportMissingTypeStubs=false
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportExplicitAny=false, reportAny=false
import json
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import vcr  # type: ignore[import-untyped]
from pydantic import SecretStr

from tiktok_mcp.api.posting.client import BASE_URL, PostingAPIClient
from tiktok_mcp.auth.keychain import account_key, serialize_account_record
from tiktok_mcp.observability.rate_limit_tracker import reset_tracker
from tiktok_mcp.tools import posting_writes_drafts as drafts_tools
from tiktok_mcp.tools.posting_writes_drafts import (
    DRAFT_DELETE_PATH,
    DRAFT_PUBLISH_PATH,
    delete_draft,
    list_pending_drafts,
    move_draft_to_publish,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType

ALIAS = "posting-alias"
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes" / "posting_drafts"
POSTING_DRAFTS_VCR = vcr.VCR(
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
def reset_posting_draft_rate_limits() -> Iterator[None]:
    reset_tracker()
    yield
    reset_tracker()


@pytest.mark.asyncio
async def test_list_pending_drafts_replay_not_gated() -> None:
    expected = _cassette_data("list.yaml")

    response = await list_pending_drafts(ALIAS)

    assert response == expected
    assert "writes_disabled" not in response


@pytest.mark.asyncio
async def test_publish_draft_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _cassette_response("publish_public.yaml", request)

    _install_client(monkeypatch, backend, handler)
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    result = await move_draft_to_publish(
        "draft-fixture-1",
        {"title": "QA test", "privacy_level": "SELF_ONLY"},
    )

    assert result["status"] in {"PROCESSING_UPLOAD", "PUBLISH_COMPLETE"}
    assert requests[0].url.path == DRAFT_PUBLISH_PATH
    assert requests[0].headers["authorization"] == "Bearer posting-access"
    assert _json_body(requests[0]) == {
        "publish_id": "draft-fixture-1",
        "post_info": {"title": "QA test", "privacy_level": "SELF_ONLY"},
    }


@pytest.mark.asyncio
async def test_delete_draft_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryBackend()
    await _store_account(backend)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _cassette_response("delete.yaml", request)

    _install_client(monkeypatch, backend, handler)
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "posting")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    result = await delete_draft("draft-fixture-1")

    assert result == {"publish_id": "draft-fixture-1", "deleted": True}
    assert requests[0].url.path == DRAFT_DELETE_PATH
    assert requests[0].headers["authorization"] == "Bearer posting-access"
    assert _json_body(requests[0]) == {"publish_id": "draft-fixture-1"}


def test_posting_drafts_vcr_config_scrubs_authorization() -> None:
    assert POSTING_DRAFTS_VCR.filter_headers == [("Authorization", "REDACTED")]


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    backend: MemoryBackend,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    async def fake_get_backend() -> MemoryBackend:
        return backend

    def build_posting_client() -> PostingAPIClient:
        return PostingAPIClient(
            backend=backend,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            base_url=BASE_URL,
        )

    monkeypatch.setattr(drafts_tools, "get_backend", fake_get_backend)
    monkeypatch.setattr(drafts_tools, "_build_posting_client", build_posting_client)


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


def _cassette_response(name: str, request: httpx.Request) -> httpx.Response:
    interaction = _first_interaction(name)
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


def _cassette_data(name: str) -> dict[str, object]:
    interaction = _first_interaction(name)
    response = cast(dict[str, Any], interaction["response"])
    body = cast(dict[str, object], response["body"])
    raw_body = body.get("string", "{}")
    if not isinstance(raw_body, str):
        raise TypeError("cassette body must be a JSON string")
    payload = cast(dict[str, object], json.loads(raw_body))
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise TypeError("cassette data must be a JSON object")
    return {str(key): value for key, value in data.items()}


def _first_interaction(name: str) -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    payload = yaml.safe_load((CASSETTE_DIR / name).read_text(encoding="utf-8"))
    interactions = cast(list[dict[str, Any]], payload["interactions"])
    return interactions[0]


def _single_value_headers(raw_headers: Mapping[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, list) and value:
            headers[str(key)] = str(value[0])
        elif value is not None:
            headers[str(key)] = str(value)
    return headers


def _json_body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content.decode("utf-8")))
