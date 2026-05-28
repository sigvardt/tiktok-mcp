from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAny=false
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.tools import marketing_reports as marketing_reports_tools
from tiktok_mcp.tools.marketing_reports import (
    ADVERTISER_INFO_PATH,
    ASYNC_REPORT_CHECK_PATH,
    ASYNC_REPORT_CREATE_PATH,
    SYNC_REPORT_PATH,
    ReportType,
    marketing_download_async_report,
    marketing_poll_async_report,
    marketing_run_async_report,
    marketing_run_sync_report,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "advertiser-123"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sync_report_returns_rows_with_currency_and_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == SYNC_REPORT_PATH
        assert request.headers["Access-Token"] == "marketing-access-token"
        assert "authorization" not in request.headers
        assert json.loads(request.url.params["dimensions"]) == ["ad_id"]
        assert json.loads(request.url.params["metrics"]) == ["spend", "impressions"]
        return _business_response(
            request,
            {
                "list": [
                    {
                        "ad_id": "ad-1",
                        "spend": "12.34",
                        "impressions": "1000",
                        "currency_code": "USD",
                        "timezone": "America/Los_Angeles",
                    }
                ],
                "page_info": {"page": 1, "page_size": 20, "total_number": 1},
            },
        )

    requests = _install_business_client(monkeypatch, handler)

    response = await marketing_run_sync_report(
        ALIAS,
        ADVERTISER_ID,
        "BASIC",
        "AUCTION_AD",
        ["ad_id"],
        ["spend", "impressions"],
        "2026-05-01",
        "2026-05-07",
    )

    rows = cast(list[dict[str, object]], response["list"])
    assert len(requests) == 1
    assert rows[0]["currency_code"] == "USD"
    assert rows[0]["timezone"] == "America/Los_Angeles"
    assert all("currency_code" in row and "timezone" in row for row in rows)
    assert response["page_info"] == {"page": 1, "page_size": 20, "total_number": 1}


@pytest.mark.asyncio
async def test_sync_report_rejects_rows_missing_currency_or_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == ADVERTISER_INFO_PATH:
            return _business_response(request, {"list": []})
        return _business_response(request, {"list": [{"ad_id": "ad-1", "spend": "1.00"}]})

    requests = _install_business_client(monkeypatch, handler)

    with pytest.raises(ValueError, match="currency_code, timezone"):
        await marketing_run_sync_report(
            ALIAS,
            ADVERTISER_ID,
            "BASIC",
            "AUCTION_AD",
            ["ad_id"],
            ["spend"],
            "2026-05-01",
            "2026-05-07",
        )

    assert [request.url.path for request in requests] == [SYNC_REPORT_PATH, ADVERTISER_INFO_PATH]


@pytest.mark.asyncio
async def test_sync_report_enriches_missing_currency_and_timezone_from_advertiser_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == SYNC_REPORT_PATH:
            return _business_response(
                request,
                {
                    "list": [
                        {
                            "dimensions": {"advertiser_id": ADVERTISER_ID},
                            "metrics": {
                                "spend": "12.34",
                                "impressions": "1000",
                            },
                        }
                    ],
                    "page_info": {"page": 1, "page_size": 20, "total_number": 1},
                },
            )

        assert request.url.path == ADVERTISER_INFO_PATH
        assert json.loads(request.url.params["advertiser_ids"]) == [ADVERTISER_ID]
        assert json.loads(request.url.params["fields"]) == [
            "advertiser_id",
            "currency",
            "display_timezone",
            "timezone",
        ]
        return _business_response(
            request,
            {
                "list": [
                    {
                        "advertiser_id": ADVERTISER_ID,
                        "currency": "DKK",
                        "display_timezone": "Europe/Copenhagen",
                    }
                ]
            },
        )

    requests = _install_business_client(monkeypatch, handler)

    response = await marketing_run_sync_report(
        ALIAS,
        ADVERTISER_ID,
        "BASIC",
        "AUCTION_ADVERTISER",
        ["advertiser_id"],
        ["spend", "impressions"],
        "2026-05-21",
        "2026-05-27",
    )

    rows = cast(list[dict[str, object]], response["list"])
    assert [request.url.path for request in requests] == [SYNC_REPORT_PATH, ADVERTISER_INFO_PATH]
    assert rows == [
        {
            "dimensions": {"advertiser_id": ADVERTISER_ID},
            "metrics": {"spend": "12.34", "impressions": "1000"},
            "currency_code": "DKK",
            "timezone": "Europe/Copenhagen",
        }
    ]


@pytest.mark.asyncio
async def test_sync_report_accepts_tiktok_currency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == SYNC_REPORT_PATH
        return _business_response(
            request,
            {
                "list": [
                    {
                        "dimensions": {"advertiser_id": ADVERTISER_ID},
                        "metrics": {
                            "spend": "12.34",
                            "currency": "DKK",
                            "timezone": "Europe/Copenhagen",
                        },
                    }
                ],
            },
        )

    requests = _install_business_client(monkeypatch, handler)

    response = await marketing_run_sync_report(
        ALIAS,
        ADVERTISER_ID,
        "BASIC",
        "AUCTION_ADVERTISER",
        ["advertiser_id"],
        ["spend", "currency", "timezone"],
        "2026-05-21",
        "2026-05-27",
    )

    rows = cast(list[dict[str, object]], response["list"])
    assert len(requests) == 1
    assert rows[0]["currency_code"] == "DKK"
    assert rows[0]["timezone"] == "Europe/Copenhagen"


@pytest.mark.asyncio
async def test_async_report_lifecycle_polls_and_downloads_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poll_count = 0
    file_url = "https://download.example.test/report.csv?signature=opaque"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == ASYNC_REPORT_CREATE_PATH:
            assert request.method == "POST"
            body = json.loads(request.content.decode("utf-8"))
            assert body["report_type"] == "BASIC"
            assert body["dimensions"] == ["ad_id"]
            return _business_response(request, {"task_id": "task-123"})

        assert request.url.path == ASYNC_REPORT_CHECK_PATH
        assert request.url.params["advertiser_id"] == ADVERTISER_ID
        assert request.url.params["task_id"] == "task-123"
        poll_count += 1
        if poll_count == 1:
            return _business_response(
                request,
                {"status": "queued", "progress_percentage": 20},
            )
        return _business_response(
            request,
            {
                "status": "success",
                "progress_percentage": 100,
                "file_url": file_url,
                "expires_at": "2026-05-22T13:00:00Z",
            },
        )

    business_requests = _install_business_client(monkeypatch, handler)
    csv_text = "ad_id,spend,currency_code,timezone\nad-1,12.34,USD,UTC\nad-2,5.00,USD,UTC\n"
    download_requests = _install_download_client(monkeypatch, csv_text)

    created = await marketing_run_async_report(
        ALIAS,
        ADVERTISER_ID,
        "BASIC",
        "AUCTION_AD",
        ["ad_id"],
        ["spend"],
        "2026-05-01",
        "2026-05-07",
    )
    queued = await marketing_poll_async_report(ALIAS, ADVERTISER_ID, "task-123")
    ready = await marketing_poll_async_report(ALIAS, ADVERTISER_ID, "task-123")
    downloaded = await marketing_download_async_report(ALIAS, ADVERTISER_ID, "task-123")

    assert created == {"task_id": "task-123", "status": "queued"}
    assert queued == {
        "status": "queued",
        "progress_percentage": 20,
        "file_url": None,
        "expires_at": None,
    }
    assert ready == {
        "status": "success",
        "progress_percentage": 100,
        "file_url": file_url,
        "expires_at": "2026-05-22T13:00:00Z",
    }
    assert downloaded["row_count"] == 2
    assert downloaded["currency_code"] == "USD"
    assert downloaded["timezone"] == "UTC"
    assert downloaded["rows"] == [
        {"ad_id": "ad-1", "spend": "12.34", "currency_code": "USD", "timezone": "UTC"},
        {"ad_id": "ad-2", "spend": "5.00", "currency_code": "USD", "timezone": "UTC"},
    ]
    assert [request.url.path for request in business_requests] == [
        ASYNC_REPORT_CREATE_PATH,
        ASYNC_REPORT_CHECK_PATH,
        ASYNC_REPORT_CHECK_PATH,
        ASYNC_REPORT_CHECK_PATH,
    ]
    assert len(download_requests) == 1
    assert str(download_requests[0].url) == file_url


@pytest.mark.asyncio
async def test_invalid_report_type_raises_validation_error_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)

    with pytest.raises(ValidationError, match="report_type"):
        await marketing_run_sync_report(
            ALIAS,
            ADVERTISER_ID,
            cast(ReportType, "NOT_A_REPORT"),
            "AUCTION_AD",
            ["ad_id"],
            ["spend"],
            "2026-05-01",
            "2026-05-07",
        )

    assert build_calls == []


@pytest.mark.asyncio
async def test_audience_report_rejects_over_30_day_range_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_calls = _install_forbidden_business_client(monkeypatch)

    with pytest.raises(ValidationError, match="AUDIENCE reports are limited to 30 days"):
        await marketing_run_async_report(
            ALIAS,
            ADVERTISER_ID,
            "AUDIENCE",
            "AUCTION_ADGROUP",
            ["adgroup_id"],
            ["spend"],
            "2026-05-01",
            "2026-06-01",
        )

    assert build_calls == []


def test_all_report_tools_are_marked_read_only() -> None:
    for tool in (
        marketing_run_sync_report,
        marketing_run_async_report,
        marketing_poll_async_report,
        marketing_download_async_report,
    ):
        assert getattr(tool, "__tiktok_mcp_read_only__", False) is True


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

    monkeypatch.setattr(marketing_reports_tools, "_build_business_client", build_client)
    return requests


def _install_forbidden_business_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    build_calls: list[str] = []

    async def build_client(alias: str) -> BusinessAPIClient:
        build_calls.append(alias)
        raise AssertionError("BusinessAPIClient must not be built after validation failure")

    monkeypatch.setattr(marketing_reports_tools, "_build_business_client", build_client)
    return build_calls


def _install_download_client(
    monkeypatch: pytest.MonkeyPatch,
    csv_text: str,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=csv_text, request=request)

    def build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(marketing_reports_tools, "_build_download_http_client", build_client)
    return requests


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
        scopes=["business.report.read"],
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
