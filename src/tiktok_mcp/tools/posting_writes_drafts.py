"""Draft-management tools for TikTok Content Posting.

TikTok exposes upload/status primitives publicly but still does not expose a v2
draft-list endpoint. `list_pending_drafts` is therefore a read-only alias over
T18's `posting_list_drafts` gap report, while draft publish/delete write tools
use the PostingAPIClient request surface for replay-tested write contracts.
"""

from __future__ import annotations

# pyright: reportMissingTypeStubs=false
from collections.abc import Mapping
from types import TracebackType
from typing import ClassVar, Literal, Protocol, Self, cast

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from tiktok_mcp.api.posting import PostingAPIClient
from tiktok_mcp.auth.keychain import get_backend
from tiktok_mcp.decorators import mark_read_only, require_writes_enabled
from tiktok_mcp.server import app
from tiktok_mcp.tools.posting_read import posting_list_drafts
from tiktok_mcp.types.accounts import ApiType
from tiktok_mcp.types.errors import AccountNotFoundError

PrivacyLevel = Literal[
    "MUTUAL_FOLLOW_FRIENDS",
    "SELF_ONLY",
    "PUBLIC_TO_EVERYONE",
    "FOLLOWER_OF_CREATOR",
]

DRAFT_PUBLISH_PATH = "/v2/post/publish/draft/publish/"
DRAFT_DELETE_PATH = "/v2/post/publish/draft/delete/"


class PostingDraftClient(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def request(
        self,
        alias: str,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
    ) -> dict[str, object]: ...


class DraftPostInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    privacy_level: PrivacyLevel
    disable_duet: bool | None = None
    disable_comment: bool | None = None
    disable_stitch: bool | None = None
    video_cover_timestamp_ms: int | None = Field(default=None, ge=0)
    auto_add_music: bool | None = None


class DraftActionParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    publish_id: str = Field(min_length=1)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def move_draft_to_publish(
    publish_id: str,
    post_info: dict[str, object],
) -> dict[str, object]:
    """Convert one Content Posting inbox draft into a published TikTok post."""
    params = DraftActionParams.model_validate({"publish_id": publish_id})
    validated_post_info = DraftPostInfo.model_validate(post_info)
    body: dict[str, object] = {
        "publish_id": params.publish_id,
        "post_info": validated_post_info.model_dump(mode="json", exclude_none=True),
    }

    alias = await _single_content_posting_alias()
    async with _build_posting_client() as client:
        payload = await client.request(alias, "POST", DRAFT_PUBLISH_PATH, json_body=body)
    return {"publish_id": params.publish_id, **payload}


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def delete_draft(publish_id: str) -> dict[str, object]:
    """Irreversibly cancel and remove one Content Posting inbox draft."""
    params = DraftActionParams.model_validate({"publish_id": publish_id})
    alias = await _single_content_posting_alias()
    async with _build_posting_client() as client:
        payload = await client.request(
            alias,
            "POST",
            DRAFT_DELETE_PATH,
            json_body={"publish_id": params.publish_id},
        )
    return {"publish_id": params.publish_id, **payload}


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def list_pending_drafts(
    alias: str,
    max_count: int = 20,
    cursor: int | None = None,
) -> dict[str, object]:
    """Read-only alias for T18's draft-list gap-reporting tool.

    TikTok has not exposed a public v2 drafts-list endpoint, so this mirrors
    `posting_list_drafts` and adds the requested alias for draft-management
    cohesion instead of guessing an undocumented endpoint.
    """
    response = cast(
        dict[str, object],
        await posting_list_drafts(alias, max_count=max_count, cursor=cursor),
    )
    if response.get("endpoint_not_available") is True:
        return {**response, "alias": alias}
    return response


def _build_posting_client() -> PostingDraftClient:
    return cast(PostingDraftClient, PostingAPIClient())


async def _single_content_posting_alias() -> str:
    backend = await get_backend()
    aliases: set[str] = set()
    marker = "::account::"
    for key in await backend.list_keys("tiktok-mcp::content_posting::"):
        if marker in key:
            aliases.add(key.rsplit(marker, 1)[1])

    if len(aliases) == 1:
        return next(iter(aliases))
    if not aliases:
        raise AccountNotFoundError("", api_type=ApiType.CONTENT_POSTING.value)

    alias_list = ", ".join(sorted(aliases))
    raise ValueError(
        "Content Posting draft writes require exactly one stored posting account "
        + f"because the T28 tool signature has no alias; found: {alias_list}"
    )


__all__ = [
    "DRAFT_DELETE_PATH",
    "DRAFT_PUBLISH_PATH",
    "DraftPostInfo",
    "delete_draft",
    "list_pending_drafts",
    "move_draft_to_publish",
]
