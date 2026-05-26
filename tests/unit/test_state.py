"""Tests for OAuth in-progress state management."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time

from tiktok_mcp.auth.state import (
    cleanup_expired,
    consume_state,
    create_state,
    get_state_count,
    reset_state_manager,
)
from tiktok_mcp.types import ApiType, OAuthStateInvalidError


@pytest.fixture(autouse=True)
def clean_state_manager() -> Iterator[None]:
    reset_state_manager()
    yield
    reset_state_manager()


@pytest.mark.asyncio
async def test_create_then_consume_returns_same_oauth_in_progress() -> None:
    """Creating then consuming returns the same OAuthInProgress values."""
    oauth_state = await create_state(
        ApiType.DISPLAY,
        "no-display-abcd",
        pkce_verifier="verifier-123",
    )

    consumed_state = await consume_state(oauth_state.state)

    assert consumed_state.state == oauth_state.state
    assert consumed_state.api_type == ApiType.DISPLAY
    assert consumed_state.sandbox is False
    assert consumed_state.pkce_verifier == "verifier-123"
    assert consumed_state.suggested_alias == "no-display-abcd"
    assert await get_state_count() == 0


@pytest.mark.asyncio
async def test_create_then_consume_round_trips_sandbox() -> None:
    oauth_state = await create_state(ApiType.DISPLAY, "no-display-sandbox", sandbox=True)

    consumed_state = await consume_state(oauth_state.state)

    assert consumed_state.sandbox is True


@pytest.mark.asyncio
async def test_double_consume_raises_replay_or_unknown() -> None:
    """A second consume is rejected as replay or unknown."""
    oauth_state = await create_state(ApiType.MARKETING, "no-marketing-abcd")

    _ = await consume_state(oauth_state.state)

    with pytest.raises(OAuthStateInvalidError) as exc_info:
        _ = await consume_state(oauth_state.state)

    assert exc_info.value.reason in {"replay", "unknown"}


@pytest.mark.asyncio
async def test_expired_state_raises_expired() -> None:
    """Consuming after the 10-minute TTL raises expired."""
    with freeze_time("2026-01-01 00:00:00", tz_offset=0) as frozen:
        oauth_state = await create_state(ApiType.DISPLAY, "no-display-abcd")
        assert oauth_state.expires_at == datetime(2026, 1, 1, 0, 10, tzinfo=UTC)

        _ = frozen.tick(timedelta(minutes=11))
        with pytest.raises(OAuthStateInvalidError) as exc_info:
            _ = await consume_state(oauth_state.state)

    assert exc_info.value.reason == "expired"


@pytest.mark.asyncio
async def test_unknown_state_raises_unknown() -> None:
    """A never-created state raises unknown."""
    with pytest.raises(OAuthStateInvalidError) as exc_info:
        _ = await consume_state("never-created-state")

    assert exc_info.value.reason == "unknown"


@pytest.mark.asyncio
async def test_concurrent_creation_yields_distinct_states() -> None:
    """Concurrent creation yields one unique state per request."""
    results = await asyncio.gather(
        *(create_state(ApiType.DISPLAY, f"alias-{index}") for index in range(100))
    )

    assert len({oauth_state.state for oauth_state in results}) == 100
    assert await get_state_count() == 100


@pytest.mark.asyncio
async def test_cleanup_expired_removes_only_expired() -> None:
    """Cleanup removes expired states while keeping active states."""
    with freeze_time("2026-01-01 00:00:00", tz_offset=0) as frozen:
        first = await create_state(ApiType.DISPLAY, "first-alias")
        second = await create_state(ApiType.MARKETING, "second-alias")

        _ = frozen.tick(timedelta(minutes=11))
        active = await create_state(ApiType.CONTENT_POSTING, "active-alias")

        cleaned_count = await cleanup_expired()
        assert cleaned_count == 2
        assert await get_state_count() == 1
        assert (await consume_state(active.state)).state == active.state

        with pytest.raises(OAuthStateInvalidError) as first_exc_info:
            _ = await consume_state(first.state)
        with pytest.raises(OAuthStateInvalidError) as second_exc_info:
            _ = await consume_state(second.state)

    assert first_exc_info.value.reason == "replay"
    assert second_exc_info.value.reason == "replay"


@pytest.mark.asyncio
async def test_state_token_entropy_url_safe() -> None:
    """Generated state tokens are unique URL-safe high-entropy strings."""
    token_pattern = re.compile(r"^[A-Za-z0-9_-]+$")
    oauth_states = [await create_state(ApiType.DISPLAY, f"alias-{index}") for index in range(100)]

    tokens = {oauth_state.state for oauth_state in oauth_states}
    assert len(tokens) == 100
    assert all(token_pattern.fullmatch(oauth_state.state) for oauth_state in oauth_states)
    assert all(len(oauth_state.state) >= 32 for oauth_state in oauth_states)


@pytest.mark.asyncio
async def test_replay_detection_after_consume() -> None:
    """A consumed state is reported specifically as replay."""
    oauth_state = await create_state(ApiType.DISPLAY, "no-display-abcd")

    _ = await consume_state(oauth_state.state)
    with pytest.raises(OAuthStateInvalidError) as exc_info:
        _ = await consume_state(oauth_state.state)

    assert exc_info.value.reason == "replay"
