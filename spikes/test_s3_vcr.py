from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingTypeArgument=false, reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false, reportUntypedFunctionDecorator=false
# pyright: reportUnknownParameterType=false, reportUnknownArgumentType=false, reportUnusedCallResult=false

import json
import os
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx", reason="Install with: uvx --with httpx ...")
pytest.importorskip("vcr", reason="Install with: uvx --with vcrpy ...")

from spikes.s3_vcr import (  # noqa: E402
    ADVERTISER_INFO_PATH,
    BUSINESS_API_BASE,
    DEFAULT_CASSETTE_PATH,
    BusinessApiError,
    configured_vcr,
    decode_business_response,
    record_invalid_call,
    verify_cassette_no_leaks,
)


CASSETTE_MISSING_MESSAGE = (
    f"{DEFAULT_CASSETTE_PATH} not found; operator must record it first per spikes/s3_results.md"
)


@pytest.fixture
def cassette_path() -> str:
    return DEFAULT_CASSETTE_PATH


def test_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKTOK_BUSINESS_PROD_TOKEN", raising=False)
    token = os.getenv("TIKTOK_BUSINESS_SANDBOX_TOKEN")
    if not token:
        pytest.skip(
            "TIKTOK_BUSINESS_SANDBOX_TOKEN is unset; operator must set a sandbox token to record"
        )
    assert token is not None, "pytest.skip should stop execution when sandbox token is unset"

    body = record_invalid_call(token)
    cassette = Path(DEFAULT_CASSETTE_PATH)

    assert cassette.exists(), f"Expected vcrpy to create cassette at {DEFAULT_CASSETTE_PATH}"
    assert cassette.stat().st_size > 0, "Expected recorded cassette to be non-empty"
    assert body.get("code") != 0, "Expected deliberately invalid Business API call to return code != 0"
    assert verify_cassette_no_leaks(DEFAULT_CASSETTE_PATH), (
        "Cassette contains a mandatory token leak pattern; do NOT git-add it"
    )


def test_replay(cassette_path: str) -> None:
    _skip_if_cassette_missing(cassette_path)

    status_code, body = _replay_invalid_call()

    assert status_code == 200, "Expected replayed Business API envelope to preserve HTTP 200"
    assert body.get("code") != 0, "Expected replayed body to preserve code != 0 business error"
    with pytest.raises(BusinessApiError) as exc_info:
        decode_business_response(body)

    error = exc_info.value
    assert error.code == body["code"], "BusinessApiError.code should mirror body['code']"
    assert error.request_id, "BusinessApiError.request_id should be non-empty for TikTok support"
    assert error.request_id == body.get("request_id"), (
        "BusinessApiError.request_id should mirror body['request_id']"
    )


def test_determinism(cassette_path: str) -> None:
    _skip_if_cassette_missing(cassette_path)

    errors = []
    for _ in range(10):
        _status_code, body = _replay_invalid_call()
        with pytest.raises(BusinessApiError) as exc_info:
            decode_business_response(body)
        error = exc_info.value
        errors.append((error.code, error.message, error.request_id))

    first_error = errors[0]
    assert all(error == first_error for error in errors), (
        "Expected 10 replayed BusinessApiError tuples to be identical "
        f"but got {errors!r}"
    )


@configured_vcr().use_cassette(DEFAULT_CASSETTE_PATH, record_mode="none")
def _replay_invalid_call() -> tuple[int, dict]:
    response = httpx.get(
        f"{BUSINESS_API_BASE}{ADVERTISER_INFO_PATH}",
        headers={"Access-Token": "REDACTED"},
        params={"advertiser_ids": json.dumps(["doesnotexist"], separators=(",", ":"))},
        timeout=30.0,
    )
    return response.status_code, response.json()


def _skip_if_cassette_missing(cassette_path: str) -> None:
    if not Path(cassette_path).exists():
        pytest.skip(CASSETTE_MISSING_MESSAGE)
