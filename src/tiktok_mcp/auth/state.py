"""In-memory OAuth state registry with TTL and replay protection."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

from tiktok_mcp.types import ApiType, OAuthInProgress, OAuthStateInvalidError

_STATES: dict[str, OAuthInProgress] = {}
_RECENTLY_CONSUMED: dict[str, datetime] = {}
_LOCK = asyncio.Lock()
_MAX_RECENTLY_CONSUMED = 1000
_TTL_SECONDS = 600


async def create_state(
    api_type: ApiType,
    suggested_alias: str,
    pkce_verifier: str | None = None,
) -> OAuthInProgress:
    async with _LOCK:
        state = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=_TTL_SECONDS)
        oauth_state = OAuthInProgress(
            state=state,
            api_type=api_type,
            pkce_verifier=pkce_verifier,
            suggested_alias=suggested_alias,
            expires_at=expires_at,
        )
        _STATES[state] = oauth_state
        return oauth_state


async def consume_state(state: str) -> OAuthInProgress:
    async with _LOCK:
        now = datetime.now(UTC)
        if state in _RECENTLY_CONSUMED:
            raise OAuthStateInvalidError(reason="replay")

        oauth_state = _STATES.get(state)
        if oauth_state is None:
            raise OAuthStateInvalidError(reason="unknown")

        if oauth_state.expires_at < now:
            _ = _STATES.pop(state)
            _add_recently_consumed(state, now)
            raise OAuthStateInvalidError(reason="expired")

        consumed_state = _STATES.pop(state)
        _add_recently_consumed(state, now)
        return consumed_state


async def cleanup_expired() -> int:
    async with _LOCK:
        now = datetime.now(UTC)
        expired_states = [
            state for state, oauth_state in _STATES.items() if oauth_state.expires_at < now
        ]

        for state in expired_states:
            _ = _STATES.pop(state)
            _add_recently_consumed(state, now)

        return len(expired_states)


async def get_state_count() -> int:
    return len(_STATES)


def reset_state_manager() -> None:
    """Clear all in-memory OAuth state registry data; test-only helper."""
    _STATES.clear()
    _RECENTLY_CONSUMED.clear()


def _add_recently_consumed(state: str, consumed_at: datetime) -> None:
    _RECENTLY_CONSUMED[state] = consumed_at
    while len(_RECENTLY_CONSUMED) > _MAX_RECENTLY_CONSUMED:
        del _RECENTLY_CONSUMED[next(iter(_RECENTLY_CONSUMED))]
