from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx
import pytest

from tiktok_mcp.api.business.urls import BUSINESS_PROD_URL
from tiktok_mcp.auth.http_sanitizer import (
    SanitizedHttpxError,
    install_httpx_sanitization,
    safe_raise_for_status,
)
from tiktok_mcp.auth.redactor import SecretRedactor, install_redactor, register_token


@pytest.fixture(autouse=True)
def clean_root_redactor() -> Iterator[None]:
    root_logger = logging.getLogger()
    original_filters = list(root_logger.filters)
    root_logger.filters = [
        logging_filter
        for logging_filter in root_logger.filters
        if not isinstance(logging_filter, SecretRedactor)
    ]
    yield
    root_logger.filters = original_filters


def test_runtime_token_never_in_caplog(caplog: pytest.LogCaptureFixture) -> None:
    """Registered runtime tokens are replaced before caplog sees them."""
    token = "supersecrettoken1234567890"
    register_token(token, "access_token")

    with caplog.at_level(logging.INFO):
        logging.getLogger().info("received runtime token %s", token)

    assert "<REDACTED:access_token>" in caplog.text
    assert token not in caplog.text


def test_auth_header_pattern(caplog: pytest.LogCaptureFixture) -> None:
    """Authorization Bearer headers are masked without token registration."""
    token = "xyzABC123secret789"
    _ = install_redactor()

    with caplog.at_level(logging.INFO):
        logging.getLogger().info("Authorization: Bearer %s", token)

    assert "Authorization: Bearer <REDACTED>" in caplog.text
    assert token not in caplog.text


def test_business_access_token_header_pattern(caplog: pytest.LogCaptureFixture) -> None:
    """Business API Access-Token headers are masked without token registration."""
    token = "abc123secret456"
    _ = install_redactor()

    with caplog.at_level(logging.INFO):
        logging.getLogger().info("Access-Token: %s", token)

    assert "Access-Token: <REDACTED>" in caplog.text
    assert token not in caplog.text


def test_args_dict_form(caplog: pytest.LogCaptureFixture) -> None:
    """Dict-style logging args and literal message tokens are both masked."""
    token = "dictsecrettoken123"
    redactor = install_redactor()
    redactor.register_token(token, "dict_token")

    with caplog.at_level(logging.INFO):
        logging.getLogger().info(
            f"message has {token} and arg %(token)s",
            {"token": token},
        )

    assert "<REDACTED:dict_token>" in caplog.text
    assert token not in caplog.text
    assert all(token not in str(record.msg) for record in caplog.records)
    assert all(token not in str(record.args) for record in caplog.records)


def test_args_tuple_form(caplog: pytest.LogCaptureFixture) -> None:
    """Tuple-style logging args and literal message tokens are both masked."""
    token = "tuplesecrettoken123"
    redactor = install_redactor()
    redactor.register_token(token, "tuple_token")

    with caplog.at_level(logging.INFO):
        logging.getLogger().info(f"message has {token} and arg %s", token)

    assert "<REDACTED:tuple_token>" in caplog.text
    assert token not in caplog.text
    assert all(token not in str(record.msg) for record in caplog.records)
    assert all(token not in str(record.args) for record in caplog.records)


def test_install_idempotent() -> None:
    """Installing the root redactor twice leaves exactly one root filter."""
    first = install_redactor()
    second = install_redactor()

    redactor_filters = [
        logging_filter
        for logging_filter in logging.getLogger().filters
        if isinstance(logging_filter, SecretRedactor)
    ]
    assert first is second
    assert len(redactor_filters) == 1


@pytest.mark.asyncio
async def test_httpx_exception_stripped() -> None:
    """Sanitized httpx errors never stringify response body content."""
    body_marker = "secret_body_marker_token_payload_xyz"
    response = _mock_response(text=f'{{"error":"{body_marker}"}}')

    with pytest.raises(SanitizedHttpxError) as exc_info:
        await safe_raise_for_status(response)

    assert body_marker not in str(exc_info.value)


@pytest.mark.asyncio
async def test_sanitized_error_preserves_safe_context() -> None:
    """Sanitized httpx errors preserve status, path-only URL, and request ID."""
    response = _mock_response()

    with pytest.raises(SanitizedHttpxError) as exc_info:
        await safe_raise_for_status(response)

    error = exc_info.value
    assert error.status == 401
    assert error.url_path == "/open_api/v1.3/oauth/token/"
    assert "secret=query-token" not in error.url_path
    assert error.request_id == "20260522000000000000000000000000"


@pytest.mark.asyncio
async def test_install_httpx_sanitization_idempotent() -> None:
    """Installing the httpx response hook twice attaches one sanitizer hook."""
    client = httpx.AsyncClient()

    try:
        install_httpx_sanitization(client)
        install_httpx_sanitization(client)

        sanitizer_hooks = [
            hook
            for hook in client.event_hooks["response"]
            if "safe_raise_for_status" in getattr(hook, "__qualname__", "")
        ]
        assert sanitizer_hooks == [safe_raise_for_status]
    finally:
        await client.aclose()


def _mock_response(text: str = '{"error":"body"}') -> httpx.Response:
    request = httpx.Request(
        "GET",
        f"{BUSINESS_PROD_URL}/open_api/v1.3/oauth/token/?secret=query-token",
    )
    return httpx.Response(
        status_code=401,
        headers={"x-tt-logid": "20260522000000000000000000000000"},
        request=request,
        text=text,
    )
