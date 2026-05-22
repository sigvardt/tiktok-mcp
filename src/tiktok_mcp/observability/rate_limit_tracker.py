"""In-memory TikTok API rate-limit posture tracking."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, model_validator

from tiktok_mcp.types import ApiType

_WINDOW = timedelta(seconds=60)

logger = logging.getLogger(__name__)


def _new_request_deque() -> deque[datetime]:
    return deque()


@dataclass
class _PostureState:
    request_timestamps: deque[datetime] = field(default_factory=_new_request_deque)
    last_429_at: datetime | None = None
    last_retry_after_seconds: float | None = None
    projected_backoff_until: datetime | None = None
    last_request_at: datetime | None = None


class RateLimitPosture(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    api_type: ApiType
    alias: str
    last_429_at: datetime | None
    last_retry_after_seconds: float | None
    projected_backoff_until: datetime | None = None
    recent_request_count_last_60s: int
    last_request_at: datetime | None

    @model_validator(mode="after")
    def compute_projected_backoff_until(self) -> Self:
        if (
            self.projected_backoff_until is None
            and self.last_429_at is not None
            and self.last_retry_after_seconds is not None
        ):
            self.projected_backoff_until = self.last_429_at + timedelta(
                seconds=self.last_retry_after_seconds
            )
        return self


_TRACKER: dict[tuple[ApiType, str], _PostureState] = {}
_LOCK = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _prune_request_timestamps(state: _PostureState, now: datetime) -> None:
    cutoff = now - _WINDOW
    while state.request_timestamps and state.request_timestamps[0] < cutoff:
        _ = state.request_timestamps.popleft()


def _state_to_posture(
    api_type: ApiType,
    alias: str,
    state: _PostureState,
) -> RateLimitPosture:
    return RateLimitPosture(
        api_type=api_type,
        alias=alias,
        last_429_at=state.last_429_at,
        last_retry_after_seconds=state.last_retry_after_seconds,
        projected_backoff_until=state.projected_backoff_until,
        recent_request_count_last_60s=len(state.request_timestamps),
        last_request_at=state.last_request_at,
    )


async def record_request(api_type: ApiType, alias: str) -> None:
    now = _now()
    key = (api_type, alias)
    async with _LOCK:
        state = _TRACKER.setdefault(key, _PostureState())
        state.request_timestamps.append(now)
        state.last_request_at = now
        _prune_request_timestamps(state, now)


async def record_429(
    api_type: ApiType,
    alias: str,
    retry_after_seconds: float | None,
) -> None:
    now = _now()
    projected_backoff_until = (
        now + timedelta(seconds=retry_after_seconds)
        if retry_after_seconds is not None
        else None
    )
    key = (api_type, alias)
    async with _LOCK:
        state = _TRACKER.setdefault(key, _PostureState())
        state.last_429_at = now
        state.last_retry_after_seconds = retry_after_seconds
        state.projected_backoff_until = projected_backoff_until

    logger.warning(
        "TikTok API rate limit observed for api_type=%s alias=%s retry_after_seconds=%s",
        api_type.value,
        alias,
        retry_after_seconds,
    )


async def get_posture(alias: str | None = None) -> list[RateLimitPosture]:
    now = _now()
    async with _LOCK:
        matching_items = [
            (key, state)
            for key, state in _TRACKER.items()
            if alias is None or key[1] == alias
        ]
        postures: list[RateLimitPosture] = []
        for (api_type, account_alias), state in matching_items:
            _prune_request_timestamps(state, now)
            postures.append(_state_to_posture(api_type, account_alias, state))

    return sorted(postures, key=lambda posture: (posture.alias, posture.api_type.value))


def reset_tracker() -> None:
    """Test-only helper that clears all in-memory rate-limit posture state."""
    _TRACKER.clear()


__all__ = [
    "RateLimitPosture",
    "get_posture",
    "record_429",
    "record_request",
    "reset_tracker",
]
