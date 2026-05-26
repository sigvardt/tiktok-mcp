from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false, reportExplicitAny=false
# pyright: reportUnknownMemberType=false
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import vcr  # type: ignore[import-untyped]
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
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
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes"
AUDIENCE_CASSETTE_DIR = CASSETTE_DIR / "marketing_audiences"
PLAINTEXT_EMAIL = "audience.user.001@example.test"
EXPECTED_EMAIL_HASH = hashlib.sha256(PLAINTEXT_EMAIL.encode("utf-8")).hexdigest()

AUDIENCE_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Access-Token", "REDACTED")],
)


@pytest.mark.asyncio
async def test_create_custom_audience_upload_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")
    monkeypatch.setenv("HOME", str(Path.cwd()))
    monkeypatch.setenv("USERPROFILE", str(Path.cwd()))

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.lower()
        assert request.method == "POST"
        assert request.url.path == CREATE_CUSTOM_AUDIENCE_PATH
        assert request.headers["Access-Token"] == "marketing-access-token"
        assert b'name="advertiser_id"' in body
        assert ADVERTISER_ID.encode("utf-8") in body
        assert PLAINTEXT_EMAIL.encode("utf-8") not in body
        assert EXPECTED_EMAIL_HASH.encode("utf-8") in body
        return _cassette_response("create_upload.yaml", request)

    requests = _install_business_client(monkeypatch, handler)

    result = await create_custom_audience(
        ALIAS,
        ADVERTISER_ID,
        "qa-test-10",
        str(FIXTURE_PATH),
        ["email"],
    )

    assert result["custom_audience_id"] == AUDIENCE_ID
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_update_and_delete_custom_audience_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["advertiser_id"] == ADVERTISER_ID
        assert body["custom_audience_id"] == AUDIENCE_ID
        if request.url.path == UPDATE_CUSTOM_AUDIENCE_PATH:
            assert body["custom_audience_name"] == "qa-test-renamed"
            return _cassette_response("update_name.yaml", request)
        if request.url.path == DELETE_CUSTOM_AUDIENCE_PATH:
            return _cassette_response("delete.yaml", request)
        raise AssertionError(f"unexpected path {request.url.path}")

    requests = _install_business_client(monkeypatch, handler)

    updated = await update_custom_audience_name(
        ALIAS,
        ADVERTISER_ID,
        AUDIENCE_ID,
        "qa-test-renamed",
    )
    deleted = await delete_custom_audience(ALIAS, ADVERTISER_ID, AUDIENCE_ID)

    assert updated["custom_audience_id"] == AUDIENCE_ID
    assert deleted["deleted"] is True
    assert [request.url.path for request in requests] == [
        UPDATE_CUSTOM_AUDIENCE_PATH,
        DELETE_CUSTOM_AUDIENCE_PATH,
    ]


def test_audience_cassettes_scrub_access_token_and_plaintext_emails() -> None:
    assert AUDIENCE_VCR.filter_headers == [("Access-Token", "REDACTED")]
    for cassette_path in AUDIENCE_CASSETTE_DIR.glob("*.yaml"):
        cassette_text = cassette_path.read_text(encoding="utf-8").lower()
        assert "marketing-access-token" not in cassette_text
        assert PLAINTEXT_EMAIL not in cassette_text


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


def _cassette_response(name: str, request: httpx.Request) -> httpx.Response:
    yaml = pytest.importorskip("yaml")
    payload = yaml.safe_load((AUDIENCE_CASSETTE_DIR / name).read_text(encoding="utf-8"))
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
