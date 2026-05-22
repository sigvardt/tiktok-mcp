from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.envelopes import decode_business_response, decode_display_response
from tiktok_mcp.types.errors import BusinessApiError, DisplayApiError

BUSINESS_URL = "https://business-api.tiktok.com/open_api/v1.3/advertiser/info/"
DISPLAY_URL = "https://open.tiktokapis.com/v2/user/info/"
BODY_SECRET_MARKER = "secret_marker_body_token_xyz789"


class AdvertiserInfo(BaseModel):
    advertiser_id: str
    name: str


def test_business_success_returns_data() -> None:
    """Business code-zero envelopes return the inner data object."""
    response = _json_response(
        {
            "code": 0,
            "message": "OK",
            "data": {"name": "x"},
            "request_id": "req1",
        },
        url=BUSINESS_URL,
    )

    assert decode_business_response(response) == {"name": "x"}


def test_business_error_with_request_id() -> None:
    """Business code errors preserve TikTok code, message, and request ID."""
    response = _json_response(
        {"code": 40000, "message": "Invalid parameter", "request_id": "req-abc-123"},
        url=BUSINESS_URL,
    )

    with pytest.raises(BusinessApiError) as exc_info:
        _ = decode_business_response(response)

    error = exc_info.value
    assert error.tiktok_code == 40000
    assert error.request_id == "req-abc-123"
    assert error.message == "Invalid parameter"
    assert error.context["request_id"] == "req-abc-123"
    assert error.context["endpoint"] == "/open_api/v1.3/advertiser/info/"


def test_business_malformed_response() -> None:
    """Business responses missing code raise a malformed envelope error."""
    response = _json_response({"foo": "bar"}, url=BUSINESS_URL, headers={"x-tt-logid": "log1"})

    with pytest.raises(BusinessApiError) as exc_info:
        _ = decode_business_response(response)

    error = exc_info.value
    assert error.tiktok_code == -1
    assert error.request_id == "log1"
    assert "malformed" in error.message


def test_business_4xx_strips_body() -> None:
    """Business HTTP errors never expose response body content."""
    response = _raw_response(
        f'{{"error":"{BODY_SECRET_MARKER}"}}',
        status_code=401,
        url=BUSINESS_URL,
        headers={"x-tt-logid": "http-log1"},
    )

    with pytest.raises(SanitizedHttpxError) as exc_info:
        _ = decode_business_response(response)

    error = exc_info.value
    assert error.status == 401
    assert error.request_id == "http-log1"
    assert not _contains_secret(str(error))


def test_business_with_typed_data_model() -> None:
    """Business decoder validates inner data with a provided pydantic model."""
    response = _json_response(
        {
            "code": 0,
            "message": "OK",
            "data": {"advertiser_id": "adv1", "name": "Demo Advertiser"},
            "request_id": "req2",
        },
        url=BUSINESS_URL,
    )

    decoded = decode_business_response(response, data_model=AdvertiserInfo)

    assert isinstance(decoded, AdvertiserInfo)
    assert decoded.advertiser_id == "adv1"
    assert decoded.name == "Demo Advertiser"


def test_display_success_returns_data() -> None:
    """Display responses without an error payload return the inner data object."""
    response = _json_response({"data": {"open_id": "x"}}, url=DISPLAY_URL)

    assert decode_display_response(response) == {"open_id": "x"}


def test_display_error_with_known_code() -> None:
    """Display error payloads raise typed errors with log IDs preserved."""
    response = _json_response(
        {"error": {"code": "access_token_invalid", "message": "Token bad", "log_id": "log1"}},
        url=DISPLAY_URL,
    )

    with pytest.raises(DisplayApiError) as exc_info:
        _ = decode_display_response(response)

    error = exc_info.value
    assert error.error_code == "access_token_invalid"
    assert error.http_status == 200
    assert error.message == "Token bad"
    assert error.context["log_id"] == "log1"
    assert error.context["endpoint"] == "/v2/user/info/"


def test_display_error_strips_body_on_4xx() -> None:
    """Display HTTP errors never expose response body content."""
    response = _raw_response(
        f'{{"error":"{BODY_SECRET_MARKER}"}}',
        status_code=401,
        url=DISPLAY_URL,
        headers={"x-tt-logid": "display-log1"},
    )

    with pytest.raises(SanitizedHttpxError) as exc_info:
        _ = decode_display_response(response)

    error = exc_info.value
    assert error.status == 401
    assert error.request_id == "display-log1"
    assert not _contains_secret(str(error))


def _json_response(
    payload: dict[str, object],
    *,
    url: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return _raw_response(json.dumps(payload), status_code=status_code, url=url, headers=headers)


def _raw_response(
    text: str,
    *,
    url: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(
        status_code=status_code,
        content=text.encode("utf-8"),
        request=request,
        headers=headers,
    )


def _contains_secret(value: str) -> bool:
    return BODY_SECRET_MARKER in value
