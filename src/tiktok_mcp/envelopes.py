"""Response envelope models for TikTok API families."""

from __future__ import annotations

from typing import Any, ClassVar, Generic, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.types.errors import BusinessApiError, DisplayApiError, ErrorContext

T = TypeVar("T")
DataModelT = TypeVar("DataModelT", bound=BaseModel)

BUSINESS_ERROR_CODES: dict[int, str] = {
    40000: "Invalid parameter",
    40001: "Invalid request",
    40002: "Missing required parameter",
    40003: "Invalid advertiser ID",
    40004: "Rate limit exceeded",
    40100: "Invalid access token",
    40101: "Access token missing",
    40104: "Access token revoked",
    40105: "Token expired",
    40300: "Forbidden",
    50000: "Internal server error",
}

DISPLAY_ERROR_CODES: dict[str, str] = {
    "access_token_invalid": "Access token invalid",
    "scope_not_authorized": "Scope not authorized",
    "rate_limit_exceeded": "Rate limit exceeded",
    "invalid_request": "Invalid request",
    "server_error": "Server error",
}

MALFORMED_BUSINESS_RESPONSE = "malformed response — missing code field"


class BusinessApiResponse(BaseModel, Generic[T]):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    code: int
    message: str
    request_id: str | None = None
    data: T | None = None


class DisplayApiErrorPayload(BaseModel):
    code: str | None = None
    message: str | None = None
    log_id: str | None = None


class DisplayApiResponse(BaseModel, Generic[T]):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    data: T | None = None
    error: DisplayApiErrorPayload | None = None


def decode_business_response(
    response: httpx.Response,
    *,
    data_model: type[DataModelT] | None = None,
) -> DataModelT | dict[str, Any]:
    # T4's sanitizer is an async httpx hook; these sync decoders receive a
    # resolved response, so duplicate only the body-free raise shape here.
    if response.status_code >= 400:
        raise SanitizedHttpxError(
            status=response.status_code,
            url_path=response.request.url.path,
            tiktok_message=None,
            request_id=response.headers.get("x-tt-logid"),
        )

    request_id = response.headers.get("x-tt-logid")
    endpoint = response.request.url.path
    payload = _json_object_or_business_error(response, request_id, endpoint)
    if "code" not in payload:
        raise BusinessApiError(
            code=-1,
            message=MALFORMED_BUSINESS_RESPONSE,
            request_id=request_id,
            context={"endpoint": endpoint},
        )

    try:
        business_response = BusinessApiResponse[Any].model_validate(payload)
    except ValidationError:
        raise BusinessApiError(
            code=-1,
            message=MALFORMED_BUSINESS_RESPONSE,
            request_id=request_id,
            context={"endpoint": endpoint},
        ) from None

    if business_response.code != 0:
        context: ErrorContext = {"endpoint": endpoint}
        known_error = BUSINESS_ERROR_CODES.get(business_response.code)
        if known_error is not None:
            context["known_error"] = known_error
        raise BusinessApiError(
            code=business_response.code,
            message=business_response.message,
            request_id=business_response.request_id,
            context=context,
        )

    if data_model is not None:
        return data_model.model_validate(business_response.data)
    return _raw_data_dict(business_response.data)


def decode_display_response(
    response: httpx.Response,
    *,
    data_model: type[DataModelT] | None = None,
) -> DataModelT | dict[str, Any]:
    # T4's sanitizer is an async httpx hook; these sync decoders receive a
    # resolved response, so duplicate only the body-free raise shape here.
    if response.status_code >= 400:
        raise SanitizedHttpxError(
            status=response.status_code,
            url_path=response.request.url.path,
            tiktok_message=None,
            request_id=response.headers.get("x-tt-logid"),
        )

    endpoint = response.request.url.path
    try:
        payload = response.json()
        display_response = DisplayApiResponse[Any].model_validate(payload)
    except (ValueError, ValidationError):
        raise DisplayApiError(
            http_status=response.status_code,
            error_code="malformed_response",
            message="Malformed Display API response",
            context={"endpoint": endpoint},
        ) from None

    if display_response.error is not None and display_response.error.code:
        context: ErrorContext = {
            "endpoint": endpoint,
            "log_id": display_response.error.log_id,
        }
        known_error = DISPLAY_ERROR_CODES.get(display_response.error.code)
        if known_error is not None:
            context["known_error"] = known_error
        raise DisplayApiError(
            http_status=response.status_code,
            error_code=display_response.error.code,
            message=display_response.error.message or "Display API error",
            context=context,
        )

    if data_model is not None:
        return data_model.model_validate(display_response.data)
    return _raw_data_dict(display_response.data)


def _json_object_or_business_error(
    response: httpx.Response,
    request_id: str | None,
    endpoint: str,
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise BusinessApiError(
            code=-1,
            message=MALFORMED_BUSINESS_RESPONSE,
            request_id=request_id,
            context={"endpoint": endpoint},
        ) from None

    if not isinstance(payload, dict):
        raise BusinessApiError(
            code=-1,
            message=MALFORMED_BUSINESS_RESPONSE,
            request_id=request_id,
            context={"endpoint": endpoint},
        )
    return {str(key): value for key, value in payload.items()}


def _raw_data_dict(data: object) -> dict[str, Any]:
    if data is None:
        return {}
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    return {"data": data}
