# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
"""MCP read tools for TikTok Marketing report retrieval.

Date limits encoded for v0.1: BASIC reports allow 365 days, AUDIENCE and
PLAYABLE_AD reports allow 30 days. Unknown report types fall back to the
conservative 30-day default, though public tool inputs are constrained by Literal.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from datetime import date
from typing import ClassVar, Literal, Self, cast

import httpx
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.auth.http_sanitizer import install_httpx_sanitization, safe_raise_for_status
from tiktok_mcp.auth.keychain import (
    KeychainBackend,
    account_key,
    app_creds_key,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.auth.redactor import register_token
from tiktok_mcp.decorators import mark_read_only
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountNotFoundError,
    AppCredentialsNotSetError,
    KeychainUnavailableError,
)

ReportType = Literal["BASIC", "AUDIENCE", "PLAYABLE_AD"]
DataLevel = Literal[
    "AUCTION_AD",
    "AUCTION_ADGROUP",
    "AUCTION_CAMPAIGN",
    "AUCTION_ADVERTISER",
]
JsonObject = dict[str, object]
ReportFilter = JsonObject

SYNC_REPORT_PATH = "/open_api/v1.3/report/integrated/get/"
ASYNC_REPORT_CREATE_PATH = "/open_api/v1.3/report/task/create/"
ASYNC_REPORT_CHECK_PATH = "/open_api/v1.3/report/task/check/"
DEFAULT_MAX_DATE_RANGE_DAYS = 30
REPORT_MAX_DATE_RANGE_DAYS: dict[str, int] = {
    "BASIC": 365,
    "AUDIENCE": 30,
    "PLAYABLE_AD": 30,
}


class MarketingReportParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    report_type: ReportType
    data_level: DataLevel
    dimensions: list[str] = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    filters: list[ReportFilter] | None = None
    order_field: str | None = None
    order_type: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_report_date_range(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")

        max_days = REPORT_MAX_DATE_RANGE_DAYS.get(
            self.report_type,
            DEFAULT_MAX_DATE_RANGE_DAYS,
        )
        actual_days = (self.end_date - self.start_date).days
        if actual_days > max_days:
            message = (
                f"{self.report_type} reports are limited to {max_days} days; "
                + f"got {actual_days} days"
            )
            raise ValueError(message)
        return self


class MarketingReportTaskParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_run_sync_report(
    alias: str,
    advertiser_id: str,
    report_type: ReportType,
    data_level: DataLevel,
    dimensions: list[str],
    metrics: list[str],
    start_date: str,
    end_date: str,
    filters: list[ReportFilter] | None = None,
    order_field: str | None = None,
    order_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> JsonObject:
    """Run a synchronous Marketing API integrated report."""
    params = _validate_report_params(
        alias,
        advertiser_id,
        report_type,
        data_level,
        dimensions,
        metrics,
        start_date,
        end_date,
        filters,
        order_field,
        order_type,
        page,
        page_size,
    )
    async with await _build_business_client(params.alias) as client:
        payload = cast(
            JsonObject,
            await client.get(SYNC_REPORT_PATH, params=_report_query_params(params)),
        )
    return _report_payload_with_explicit_currency_timezone(payload)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_run_async_report(
    alias: str,
    advertiser_id: str,
    report_type: ReportType,
    data_level: DataLevel,
    dimensions: list[str],
    metrics: list[str],
    start_date: str,
    end_date: str,
    filters: list[ReportFilter] | None = None,
    order_field: str | None = None,
    order_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> JsonObject:
    """Create a read-only asynchronous Marketing API report task."""
    params = _validate_report_params(
        alias,
        advertiser_id,
        report_type,
        data_level,
        dimensions,
        metrics,
        start_date,
        end_date,
        filters,
        order_field,
        order_type,
        page,
        page_size,
    )
    async with await _build_business_client(params.alias) as client:
        payload = cast(
            JsonObject,
            await client.post(ASYNC_REPORT_CREATE_PATH, json=_report_json_payload(params)),
        )
    return {
        "task_id": _required_string(payload, ("task_id", "report_task_id")),
        "status": "queued",
    }


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_poll_async_report(
    alias: str,
    advertiser_id: str,
    task_id: str,
) -> JsonObject:
    """Poll an asynchronous Marketing API report task."""
    params = MarketingReportTaskParams.model_validate(
        {"alias": alias, "advertiser_id": advertiser_id, "task_id": task_id}
    )
    async with await _build_business_client(params.alias) as client:
        return await _poll_async_report(client, params)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_download_async_report(
    alias: str,
    advertiser_id: str,
    task_id: str,
) -> JsonObject:
    """Download and parse a completed asynchronous Marketing report CSV in memory."""
    params = MarketingReportTaskParams.model_validate(
        {"alias": alias, "advertiser_id": advertiser_id, "task_id": task_id}
    )
    async with await _build_business_client(params.alias) as client:
        status = await _poll_async_report(client, params)

    file_url = status.get("file_url")
    if not isinstance(file_url, str) or not file_url:
        raise ValueError("Async report task does not have a downloadable file_url yet")

    rows = _rows_with_explicit_currency_timezone(await _download_csv_rows(file_url), {})
    currency_code, timezone = _single_currency_timezone(rows)
    return {
        "rows": rows,
        "row_count": len(rows),
        "currency_code": currency_code,
        "timezone": timezone,
    }


def _validate_report_params(
    alias: str,
    advertiser_id: str,
    report_type: ReportType,
    data_level: DataLevel,
    dimensions: list[str],
    metrics: list[str],
    start_date: str,
    end_date: str,
    filters: list[ReportFilter] | None,
    order_field: str | None,
    order_type: str | None,
    page: int,
    page_size: int,
) -> MarketingReportParams:
    return MarketingReportParams.model_validate(
        {
            "alias": alias,
            "advertiser_id": advertiser_id,
            "report_type": report_type,
            "data_level": data_level,
            "dimensions": dimensions,
            "metrics": metrics,
            "start_date": start_date,
            "end_date": end_date,
            "filters": filters,
            "order_field": order_field,
            "order_type": order_type,
            "page": page,
            "page_size": page_size,
        }
    )


def _report_json_payload(params: MarketingReportParams) -> JsonObject:
    payload: JsonObject = {
        "advertiser_id": params.advertiser_id,
        "report_type": params.report_type,
        "data_level": params.data_level,
        "dimensions": params.dimensions,
        "metrics": params.metrics,
        "start_date": params.start_date.isoformat(),
        "end_date": params.end_date.isoformat(),
        "page": params.page,
        "page_size": params.page_size,
    }
    if params.filters is not None:
        payload["filters"] = params.filters
    if params.order_field is not None:
        payload["order_field"] = params.order_field
    if params.order_type is not None:
        payload["order_type"] = params.order_type
    return payload


def _report_query_params(params: MarketingReportParams) -> dict[str, str | int]:
    query_params: dict[str, str | int] = {}
    for key, value in _report_json_payload(params).items():
        if isinstance(value, str | int):
            query_params[key] = value
        else:
            query_params[key] = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return query_params


def _report_payload_with_explicit_currency_timezone(payload: JsonObject) -> JsonObject:
    rows_key, rows = _extract_report_rows(payload)
    if rows_key is None:
        return payload

    enriched_payload = dict(payload)
    enriched_payload[rows_key] = _rows_with_explicit_currency_timezone(rows, payload)
    return enriched_payload


def _extract_report_rows(payload: Mapping[str, object]) -> tuple[str | None, list[JsonObject]]:
    for rows_key in ("list", "rows"):
        raw_rows = payload.get(rows_key)
        if raw_rows is None:
            continue
        if not isinstance(raw_rows, list):
            raise ValueError(f"Report response field {rows_key} must be a list")
        rows: list[JsonObject] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                raise ValueError("Report rows must be JSON objects")
            rows.append(_object_mapping_to_json_object(cast(dict[object, object], raw_row)))
        return rows_key, rows
    return None, []


def _rows_with_explicit_currency_timezone(
    rows: list[JsonObject],
    payload: Mapping[str, object],
) -> list[JsonObject]:
    return [_row_with_explicit_currency_timezone(row, payload) for row in rows]


def _row_with_explicit_currency_timezone(
    row: Mapping[str, object],
    payload: Mapping[str, object],
) -> JsonObject:
    currency_code = _field_value(row, "currency_code")
    timezone = _field_value(row, "timezone")
    metrics = row.get("metrics")
    if isinstance(metrics, dict):
        metric_values = _object_mapping_to_json_object(cast(dict[object, object], metrics))
        if currency_code is None:
            currency_code = _field_value(metric_values, "currency_code")
        if timezone is None:
            timezone = _field_value(metric_values, "timezone")

    if currency_code is None:
        currency_code = _field_value(payload, "currency_code")
    if timezone is None:
        timezone = _field_value(payload, "timezone")

    missing_fields = [
        field_name
        for field_name, value in (("currency_code", currency_code), ("timezone", timezone))
        if value is None
    ]
    if missing_fields:
        raise ValueError("Marketing report row is missing explicit " + ", ".join(missing_fields))

    enriched_row = dict(row)
    enriched_row["currency_code"] = currency_code
    enriched_row["timezone"] = timezone
    return enriched_row


def _field_value(mapping: Mapping[str, object], key: str) -> object | None:
    value = mapping.get(key)
    return value if value is not None else None


def _object_mapping_to_json_object(mapping: Mapping[object, object]) -> JsonObject:
    return {str(key): value for key, value in mapping.items()}


async def _poll_async_report(
    client: BusinessAPIClient,
    params: MarketingReportTaskParams,
) -> JsonObject:
    payload = cast(
        JsonObject,
        await client.get(
            ASYNC_REPORT_CHECK_PATH,
            params={"advertiser_id": params.advertiser_id, "task_id": params.task_id},
        ),
    )
    return {
        "status": _optional_string(payload, ("status", "task_status")),
        "progress_percentage": _optional_number(
            payload,
            ("progress_percentage", "progress"),
        ),
        "file_url": _optional_string(payload, ("file_url", "download_url", "result_url")),
        "expires_at": _optional_string(payload, ("expires_at", "expire_time", "expired_at")),
    }


def _required_string(payload: Mapping[str, object], field_names: tuple[str, ...]) -> str:
    value = _optional_string(payload, field_names)
    if value is None:
        raise ValueError("Response is missing required field: " + " or ".join(field_names))
    return value


def _optional_string(payload: Mapping[str, object], field_names: tuple[str, ...]) -> str | None:
    for field_name in field_names:
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def _optional_number(
    payload: Mapping[str, object],
    field_names: tuple[str, ...],
) -> int | float | None:
    for field_name in field_names:
        value = payload.get(field_name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return value
    return None


async def _download_csv_rows(file_url: str) -> list[JsonObject]:
    csv_parts: list[str] = []
    async with (
        _build_download_http_client() as client,
        client.stream("GET", file_url) as response,
    ):
        await safe_raise_for_status(response)
        async for chunk in response.aiter_text():
            csv_parts.append(chunk)
    return _parse_csv_rows("".join(csv_parts))


def _build_download_http_client() -> httpx.AsyncClient:
    client = httpx.AsyncClient(timeout=30.0)
    install_httpx_sanitization(client)
    return client


def _parse_csv_rows(csv_text: str) -> list[JsonObject]:
    reader: csv.DictReader[str] = csv.DictReader(io.StringIO(csv_text))
    rows: list[JsonObject] = []
    for raw_row in reader:
        row: JsonObject = {}
        for key, value in raw_row.items():
            if key is None:
                continue
            row[str(key)] = value
        rows.append(row)
    return rows


def _single_currency_timezone(rows: list[JsonObject]) -> tuple[object | None, object | None]:
    return _single_row_value(rows, "currency_code"), _single_row_value(rows, "timezone")


def _single_row_value(rows: list[JsonObject], field_name: str) -> object | None:
    values: list[object] = []
    for row in rows:
        value = _field_value(row, field_name)
        if value is not None and value not in values:
            values.append(value)
    return values[0] if len(values) == 1 else None


async def _build_business_client(alias: str) -> BusinessAPIClient:
    backend = await get_backend()
    account, tokens = await _load_marketing_account(backend, alias)
    credentials = await _load_app_credentials(backend, account)
    return BusinessAPIClient(account, credentials, tokens=tokens, backend=backend)


async def _load_marketing_account(
    backend: KeychainBackend,
    alias: str,
) -> tuple[Account, AccountTokens]:
    for sandbox in (False, True):
        raw_record = await backend.get(account_key(ApiType.MARKETING, sandbox, alias))
        if raw_record is None:
            continue
        account, tokens = deserialize_account_record(raw_record)
        return account, tokens
    raise AccountNotFoundError(alias, api_type=ApiType.MARKETING.value)


async def _load_app_credentials(
    backend: KeychainBackend,
    account: Account,
) -> AppCredentials:
    raw_credentials = await backend.get(app_creds_key(account.api_type, account.sandbox))
    if raw_credentials is None:
        raise AppCredentialsNotSetError(account.api_type.value, account.sandbox)
    try:
        payload = cast(object, json.loads(raw_credentials))
    except json.JSONDecodeError as exc:
        raise KeychainUnavailableError("Stored app credentials are not valid JSON.") from exc
    credentials = AppCredentials.model_validate(payload)
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")
    return credentials


__all__ = [
    "marketing_download_async_report",
    "marketing_poll_async_report",
    "marketing_run_async_report",
    "marketing_run_sync_report",
]
