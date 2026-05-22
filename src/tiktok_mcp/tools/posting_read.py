"""MCP read tools for TikTok Content Posting.

Content Posting uploads default to TikTok drafts unless a later write tool opts into
direct posting explicitly. These read-only tools do not upload or publish content.
Creator info is intentionally fetched live and not cached because privacy options can
change inside the TikTok app.
"""

from __future__ import annotations

# pyright: reportMissingTypeStubs=false
from mcp.types import ToolAnnotations

from tiktok_mcp.api.posting import PostingAPIClient
from tiktok_mcp.api.posting.client import drafts_endpoint_not_available
from tiktok_mcp.decorators import mark_read_only
from tiktok_mcp.server import app


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def posting_get_post_status(alias: str, publish_id: str) -> dict[str, object]:
    """Get TikTok Content Posting upload/publish status for a publish ID."""
    async with _build_posting_client() as client:
        status = await client.get_post_status(alias, publish_id)
    return status.model_dump(mode="json")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def posting_list_drafts(
    alias: str,
    max_count: int = 20,
    cursor: int | None = None,
) -> dict[str, object]:
    """Report the public API gap for listing the user's Content Posting drafts.

    TikTok v2 documents inbox upload init and status polling, but not a read endpoint
    that lists drafts. Keep this tool registered so agents get an explicit, typed
    response instead of trying a guessed endpoint.
    """
    _ = alias, max_count, cursor
    return drafts_endpoint_not_available()


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def posting_get_creator_info(alias: str) -> dict[str, object]:
    """Get live creator privacy/capability settings required before upload init."""
    async with _build_posting_client() as client:
        creator_info = await client.get_creator_info(alias)
    return creator_info.model_dump(mode="json")


def _build_posting_client() -> PostingAPIClient:
    return PostingAPIClient()


__all__ = [
    "posting_get_creator_info",
    "posting_get_post_status",
    "posting_list_drafts",
]
