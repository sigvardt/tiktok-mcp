"""MCP tools for TikTok API rate-limit observability."""

from __future__ import annotations

from datetime import UTC, datetime

from mcp.types import ToolAnnotations

from tiktok_mcp.decorators import mark_read_only
from tiktok_mcp.observability.rate_limit_tracker import get_posture
from tiktok_mcp.server import app

_TOOL_DESCRIPTION = (
    "Returns per-account TikTok API rate-limit posture: when each account last "
    "received a 429, what the projected backoff window is, and how many requests "
    "have been made in the last 60 seconds. Useful when planning a batch of API calls."
)


@app.tool(
    description=_TOOL_DESCRIPTION,
    annotations=ToolAnnotations(readOnlyHint=True),
)
@mark_read_only
async def get_rate_limit_status(alias: str | None = None) -> dict[str, object]:
    postures = await get_posture(alias)
    accounts = [posture.model_dump(mode="json") for posture in postures]
    return {
        "accounts": accounts,
        "count": len(accounts),
        "as_of": datetime.now(UTC).isoformat(),
    }


__all__ = ["get_rate_limit_status"]
