from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false, reportPrivateUsage=false
# pyright: reportUnknownMemberType=false
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.marketing.audience_hashing import (
    hash_identifier,
    iter_hashed_audience_csv_rows,
)
from tiktok_mcp.server import app
from tiktok_mcp.tools import marketing_writes_audiences as audience_tools
from tiktok_mcp.tools.marketing_writes_audiences import (
    CREATE_CUSTOM_AUDIENCE_PATH,
    DELETE_CUSTOM_AUDIENCE_PATH,
    UPDATE_CUSTOM_AUDIENCE_PATH,
    create_custom_audience,
    delete_custom_audience,
    update_custom_audience_name,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "7642629596042543111"
AUDIENCE_ID = "audience-123"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "audiences" / "sample_emails.csv"
PLAINTEXT_EMAIL = "audience.user.001@example.test"
EXPECTED_EMAIL_HASH = hashlib.sha256(PLAINTEXT_EMAIL.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    results = [
        await create_custom_audience(
            ALIAS,
            ADVERTISER_ID,
            "QA Audience",
            str(FIXTURE_PATH),
            ["email"],
        ),
        await update_custom_audience_name(ALIAS, ADVERTISER_ID, AUDIENCE_ID, "Renamed"),
        await delete_custom_audience(ALIAS, ADVERTISER_ID, AUDIENCE_ID),
    ]

    assert [result["error"] for result in results] == ["writes_disabled"] * 3
    assert [result["api"] for result in results] == ["marketing"] * 3
    assert build_calls == []


@pytest.mark.asyncio
async def test_path_traversal_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    result = await create_custom_audience(
        ALIAS,
        ADVERTISER_ID,
        "QA Audience",
        "../../../etc/passwd",
        ["email"],
    )

    assert result["error"] == "invalid_path"
    assert build_calls == []


def test_hash_format() -> None:
    assert hash_identifier("  Audience.User.001@Example.Test  ", "email") == EXPECTED_EMAIL_HASH
    assert hash_identifier(" +1 (234) 567-8900 ", "phone") == hashlib.sha256(
        b"12345678900"
    ).hexdigest()

    hashed_rows = list(iter_hashed_audience_csv_rows(FIXTURE_PATH, ["email"]))
    assert hashed_rows[0] == "email_sha256\n"
    assert hashed_rows[1] == f"{EXPECTED_EMAIL_HASH}\n"
    assert PLAINTEXT_EMAIL not in "".join(hashed_rows).lower()


@pytest.mark.asyncio
async def test_no_pii_in_logs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setenv("HOME", str(Path.cwd()))

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.lower()
        assert request.method == "POST"
        assert request.url.path == CREATE_CUSTOM_AUDIENCE_PATH
        assert request.headers["Access-Token"] == "marketing-access-token"
        assert PLAINTEXT_EMAIL.encode("utf-8") not in body
        assert EXPECTED_EMAIL_HASH.encode("utf-8") in body
        assert b'name="file"; filename="audience.csv"' in body
        return _business_response(request, {"custom_audience_id": AUDIENCE_ID})

    requests = _install_business_client(monkeypatch, handler)

    with caplog.at_level(logging.INFO, logger="tiktok_mcp.tools.marketing_writes_audiences"):
        result = await create_custom_audience(
            ALIAS,
            ADVERTISER_ID,
            "QA Audience",
            str(FIXTURE_PATH),
            ["email"],
        )

    assert result == {"custom_audience_id": AUDIENCE_ID}
    assert len(requests) == 1
    assert PLAINTEXT_EMAIL not in caplog.text.lower()
    assert str(FIXTURE_PATH) not in caplog.text
    assert any(getattr(record, "filename_hash", None) for record in caplog.records)
    assert any(getattr(record, "row_count_estimate", None) == 10 for record in caplog.records)
    assert any(
        getattr(record, "file_size_bytes", None) == FIXTURE_PATH.stat().st_size
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_update_and_delete_custom_audience_post_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["advertiser_id"] == ADVERTISER_ID
        assert body["custom_audience_id"] == AUDIENCE_ID
        if request.url.path == UPDATE_CUSTOM_AUDIENCE_PATH:
            assert body["custom_audience_name"] == "Renamed Audience"
            return _business_response(request, {"custom_audience_id": AUDIENCE_ID, "updated": True})
        if request.url.path == DELETE_CUSTOM_AUDIENCE_PATH:
            return _business_response(request, {"custom_audience_id": AUDIENCE_ID, "deleted": True})
        raise AssertionError(f"unexpected path {request.url.path}")

    requests = _install_business_client(monkeypatch, handler)

    updated = await update_custom_audience_name(
        ALIAS,
        ADVERTISER_ID,
        AUDIENCE_ID,
        "Renamed Audience",
    )
    deleted = await delete_custom_audience(ALIAS, ADVERTISER_ID, AUDIENCE_ID)

    assert updated["updated"] is True
    assert deleted["deleted"] is True
    assert [request.url.path for request in requests] == [
        UPDATE_CUSTOM_AUDIENCE_PATH,
        DELETE_CUSTOM_AUDIENCE_PATH,
    ]


def test_audience_tools_are_destructive_and_write_gated() -> None:
    registered_tools = app._tool_manager.__dict__["_tools"]

    for tool_name, tool_function in (
        ("create_custom_audience", create_custom_audience),
        ("update_custom_audience_name", update_custom_audience_name),
        ("delete_custom_audience", delete_custom_audience),
    ):
        tool = registered_tools[tool_name]
        assert tool.annotations.destructiveHint is True
        assert tool_function.__dict__["__tiktok_mcp_destructive__"] is True
        assert tool_function.__dict__["__tiktok_mcp_write_api__"] == "marketing"


def _install_business_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def build_client(alias: str) -> BusinessAPIClient:
        assert alias == ALIAS
        return BusinessAPIClient(
            _account(),
            _credentials(),
            transport=httpx.MockTransport(recording_handler),
        )

    monkeypatch.setattr(audience_tools, "_build_business_client", build_client)
    return requests


def _install_forbidden_business_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    build_calls: list[str] = []

    async def build_client(alias: str) -> BusinessAPIClient:
        build_calls.append(alias)
        raise AssertionError("BusinessAPIClient must not be built for blocked/invalid writes")

    monkeypatch.setattr(audience_tools, "_build_business_client", build_client)
    return build_calls


def _business_response(request: httpx.Request, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "message": "OK", "request_id": "req-ok", "data": data},
        request=request,
    )


def _account() -> AccountWithTokens:
    return AccountWithTokens(
        alias=ALIAS,
        api_type=ApiType.MARKETING,
        sandbox=True,
        tiktok_id=ADVERTISER_ID,
        display_name="Marketing Demo",
        avatar_url=None,
        scopes=["business.audience.write"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
        access_token=SecretStr("marketing-access-token"),
        refresh_token=SecretStr("marketing-refresh-token"),
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_expires_at=NOW + timedelta(days=30),
        last_rotated_at=NOW,
    )


def _credentials() -> AppCredentials:
    return AppCredentials(
        api_type=ApiType.MARKETING,
        sandbox=True,
        client_id=SecretStr("marketing-client-id"),
        client_secret=SecretStr("marketing-client-secret"),
        created_at=NOW,
    )
