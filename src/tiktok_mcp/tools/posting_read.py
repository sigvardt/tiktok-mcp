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
from tiktok_mcp.api.posting.client import POST_STATUS_PATH, drafts_endpoint_not_available
from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.decorators import mark_read_only
from tiktok_mcp.server import app

UNKNOWN_PUBLISH_ID_OR_EXPIRED_MESSAGE = (
    "TikTok returned HTTP 400 for this publish_id. The id may be unknown, expired, "
    "or malformed. Verify the publish_id from a prior upload tool."
)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def posting_get_post_status(alias: str, publish_id: str) -> dict[str, object]:
    """Get TikTok Content Posting upload/publish status for a publish ID."""
    async with _build_posting_client() as client:
        try:
            status = await client.get_post_status(alias, publish_id)
        except SanitizedHttpxError as exc:
            if _is_unknown_publish_id_400(exc):
                return _unknown_publish_id_envelope(
                    tool="posting_get_post_status",
                    publish_id=publish_id,
                    request_id=exc.request_id,
                )
            raise
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


def _is_unknown_publish_id_400(exc: SanitizedHttpxError) -> bool:
    return exc.status == 400 and exc.url_path == POST_STATUS_PATH


def _unknown_publish_id_envelope(
    *,
    tool: str,
    publish_id: str,
    request_id: str | None,
) -> dict[str, object]:
    return {
        "error": "unknown_publish_id_or_expired",
        "tool": tool,
        "publish_id": publish_id,
        "message": UNKNOWN_PUBLISH_ID_OR_EXPIRED_MESSAGE,
        "request_id": request_id,
    }


__all__ = [
    "posting_get_creator_info",
    "posting_get_post_status",
    "posting_list_drafts",
]
