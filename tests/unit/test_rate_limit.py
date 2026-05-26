from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from freezegun import freeze_time

from tiktok_mcp.observability.rate_limit_tracker import (
    get_posture,
    record_429,
    record_request,
    reset_tracker,
)
from tiktok_mcp.tools.rate_limit import get_rate_limit_status
from tiktok_mcp.types import ApiType

RATE_LIMIT_FIELDS = {
    "api_type",
    "alias",
    "last_429_at",
    "last_retry_after_seconds",
    "projected_backoff_until",
    "recent_request_count_last_60s",
    "last_request_at",
}


@pytest.fixture(autouse=True)
def clean_rate_limit_tracker() -> None:
    """Rate-limit tracker state is isolated between tests."""
    reset_tracker()


@pytest.mark.asyncio
async def test_record_request_increments_counter() -> None:
    """Recorded requests increment the rolling 60s counter."""
    for _ in range(3):
        await record_request(ApiType.DISPLAY, "demo-display")

    postures = await get_posture("demo-display")

    assert len(postures) == 1
    assert postures[0].api_type is ApiType.DISPLAY
    assert postures[0].alias == "demo-display"
    assert postures[0].recent_request_count_last_60s == 3
    assert postures[0].last_request_at is not None


@pytest.mark.asyncio
async def test_record_429_updates_posture() -> None:
    """Recorded 429s expose retry-after and projected backoff posture."""
    before_recording = datetime.now(UTC)

    await record_429(ApiType.MARKETING, "demo-marketing", retry_after_seconds=5.0)

    posture = (await get_posture("demo-marketing"))[0]
    assert posture.last_429_at is not None
    assert before_recording <= posture.last_429_at <= datetime.now(UTC)
    assert posture.last_retry_after_seconds == 5.0
    assert posture.projected_backoff_until == posture.last_429_at + timedelta(seconds=5.0)


@pytest.mark.asyncio
async def test_get_posture_alias_filter() -> None:
    """Alias filtering narrows posture results across API types."""
    await record_request(ApiType.DISPLAY, "x")
    await record_request(ApiType.CONTENT_POSTING, "y")

    filtered_postures = await get_posture(alias="x")
    missing_postures = await get_posture(alias="nonexistent")
    all_postures = await get_posture()

    assert [(posture.api_type, posture.alias) for posture in filtered_postures] == [
        (ApiType.DISPLAY, "x")
    ]
    assert missing_postures == []
    assert {(posture.api_type, posture.alias) for posture in all_postures} == {
        (ApiType.DISPLAY, "x"),
        (ApiType.CONTENT_POSTING, "y"),
    }


@pytest.mark.asyncio
async def test_60s_window_pruning() -> None:
    """Rolling request counters prune timestamps older than 60 seconds."""
    start = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    with freeze_time(start) as frozen_time:
        await record_request(ApiType.BUSINESS_ORGANIC, "demo-business")
        _ = frozen_time.tick(delta=timedelta(seconds=30))
        await record_request(ApiType.BUSINESS_ORGANIC, "demo-business")
        assert (await get_posture("demo-business"))[0].recent_request_count_last_60s == 2

        _ = frozen_time.tick(delta=timedelta(seconds=40))
        posture = (await get_posture("demo-business"))[0]

    assert posture.recent_request_count_last_60s == 1


@pytest.mark.asyncio
async def test_concurrent_record_requests_thread_safe() -> None:
    """Concurrent request recordings do not lose counter increments."""
    _ = await asyncio.gather(*(record_request(ApiType.DISPLAY, "demo-display") for _ in range(50)))

    posture = (await get_posture("demo-display"))[0]

    assert posture.recent_request_count_last_60s == 50


@pytest.mark.asyncio
async def test_no_persistence() -> None:
    """Resetting the test-only tracker proves posture is memory-only."""
    await record_request(ApiType.DISPLAY, "demo-display")
    await record_429(ApiType.DISPLAY, "demo-display", retry_after_seconds=None)

    reset_tracker()

    assert await get_posture() == []


@pytest.mark.asyncio
async def test_tool_returns_expected_shape() -> None:
    """The MCP tool returns the expected status envelope and posture fields."""
    await record_request(ApiType.DISPLAY, "demo-display")
    await record_429(ApiType.DISPLAY, "demo-display", retry_after_seconds=2.5)

    response = cast(dict[str, object], await get_rate_limit_status(alias=None))

    assert set(response) == {"accounts", "count", "as_of"}
    assert response["count"] == 1
    assert datetime.fromisoformat(str(response["as_of"])).tzinfo is not None
    accounts = cast(list[dict[str, object]], response["accounts"])
    assert len(accounts) == 1
    assert set(accounts[0]) == RATE_LIMIT_FIELDS
    assert accounts[0]["api_type"] == ApiType.DISPLAY.value
    assert accounts[0]["alias"] == "demo-display"
