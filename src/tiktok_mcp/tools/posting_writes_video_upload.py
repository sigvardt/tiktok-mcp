from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingTypeStubs=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
import asyncio
import base64
import binascii
from dataclasses import dataclass
from typing import Self, cast

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, model_validator

from tiktok_mcp.api.posting import PostingAPIClient, PostPublishStatus, PostStatus
from tiktok_mcp.api.posting.chunker import chunk_bounds_for_index, chunk_bytes_for_upload
from tiktok_mcp.api.posting.client import POST_STATUS_PATH
from tiktok_mcp.decorators import require_writes_enabled
from tiktok_mcp.server import app

INIT_VIDEO_UPLOAD_PATH = "/v2/post/publish/inbox/video/init/"
STATUS_POLL_ATTEMPTS = 30
STATUS_POLL_INTERVAL_SECONDS = 0.1
TERMINAL_STATUSES = frozenset(
    {
        PostPublishStatus.PUBLISH_COMPLETE,
        PostPublishStatus.FAILED,
        PostPublishStatus.EXPIRED,
    }
)


@dataclass(frozen=True)
class VideoUploadSession:
    alias: str
    publish_id: str
    upload_url: str
    video_size: int
    chunk_size: int
    total_chunk_count: int


class InitVideoUploadArgs(BaseModel):
    alias: str = Field(min_length=1)
    video_size: int = Field(gt=0)
    chunk_size: int = Field(gt=0)
    total_chunk_count: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_chunk_plan(self) -> Self:
        _ = chunk_bytes_for_upload(self.video_size, self.chunk_size, self.total_chunk_count)
        return self


class UploadVideoChunkArgs(BaseModel):
    publish_id: str = Field(min_length=1)
    upload_url: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    chunk_bytes_b64: str = Field(min_length=1)


class FinalizeVideoUploadArgs(BaseModel):
    publish_id: str = Field(min_length=1)


_UPLOAD_SESSIONS: dict[str, VideoUploadSession] = {}


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def init_video_upload(
    alias: str,
    video_size: int,
    chunk_size: int,
    total_chunk_count: int,
) -> dict[str, object]:
    """Initialize a TikTok Content Posting FILE_UPLOAD draft upload."""
    args = InitVideoUploadArgs(
        alias=alias,
        video_size=video_size,
        chunk_size=chunk_size,
        total_chunk_count=total_chunk_count,
    )
    body: dict[str, object] = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": args.video_size,
            "chunk_size": args.chunk_size,
            "total_chunk_count": args.total_chunk_count,
        },
    }
    async with _build_posting_client() as client:
        data = await client.request(args.alias, "POST", INIT_VIDEO_UPLOAD_PATH, json_body=body)

    publish_id, upload_url = _upload_init_values(data)
    _UPLOAD_SESSIONS[publish_id] = VideoUploadSession(
        alias=args.alias,
        publish_id=publish_id,
        upload_url=upload_url,
        video_size=args.video_size,
        chunk_size=args.chunk_size,
        total_chunk_count=args.total_chunk_count,
    )
    return {"publish_id": publish_id, "upload_url": upload_url}


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def upload_video_chunk(
    publish_id: str,
    upload_url: str,
    chunk_index: int,
    chunk_bytes_b64: str,
) -> dict[str, object]:
    """Upload one FILE_UPLOAD video chunk to TikTok's upload URL."""
    args = UploadVideoChunkArgs(
        publish_id=publish_id,
        upload_url=upload_url,
        chunk_index=chunk_index,
        chunk_bytes_b64=chunk_bytes_b64,
    )
    session = _session_for_publish_id(args.publish_id)
    if args.upload_url != session.upload_url:
        raise ValueError("upload_url must match the URL returned by init_video_upload")

    chunk = _decode_chunk(args.chunk_bytes_b64)
    bounds = chunk_bounds_for_index(
        session.video_size,
        session.chunk_size,
        session.total_chunk_count,
        args.chunk_index,
    )
    if len(chunk) != bounds.size:
        raise ValueError(
            f"chunk {args.chunk_index} must contain exactly {bounds.size} bytes for this upload"
        )

    headers = {
        "Content-Range": bounds.content_range,
        "Content-Type": "application/octet-stream",
    }
    async with _build_posting_client() as client:
        _ = await client.put_chunk_to_url(
            session.alias,
            args.upload_url,
            headers=headers,
            content=chunk,
        )

    return {
        "publish_id": args.publish_id,
        "chunk_index": args.chunk_index,
        "status": "CHUNK_UPLOADED",
        "content_range": bounds.content_range,
        "uploaded_bytes": len(chunk),
    }


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def finalize_video_upload(publish_id: str) -> dict[str, object]:
    """Poll Content Posting status until a FILE_UPLOAD reaches a terminal status."""
    args = FinalizeVideoUploadArgs(publish_id=publish_id)
    session = _session_for_publish_id(args.publish_id)
    async with _build_posting_client() as client:
        for attempt in range(STATUS_POLL_ATTEMPTS):
            data = await client.request(
                session.alias,
                "POST",
                POST_STATUS_PATH,
                json_body={"publish_id": args.publish_id},
            )
            status = PostStatus.model_validate(data)
            if status.status in TERMINAL_STATUSES:
                result = status.model_dump(mode="json")
                result["publish_id"] = args.publish_id
                result["terminal"] = True
                return cast(dict[str, object], result)
            if attempt + 1 < STATUS_POLL_ATTEMPTS:
                await asyncio.sleep(STATUS_POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Timed out waiting for publish_id {args.publish_id} to reach terminal status"
    )


def _build_posting_client() -> PostingAPIClient:
    return PostingAPIClient()


def _session_for_publish_id(publish_id: str) -> VideoUploadSession:
    try:
        return _UPLOAD_SESSIONS[publish_id]
    except KeyError as exc:
        raise ValueError(
            "Unknown publish_id; call init_video_upload again after MCP restart"
        ) from exc


def _decode_chunk(chunk_bytes_b64: str) -> bytes:
    try:
        return base64.b64decode(chunk_bytes_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("chunk_bytes_b64 must be valid base64") from exc


def _upload_init_values(data: dict[str, object]) -> tuple[str, str]:
    publish_id = _string_value(data, "publish_id")
    upload_url = _string_value(data, "upload_url") or _nested_string_value(
        data,
        "source_info",
        "upload_url",
    )
    if publish_id is None or upload_url is None:
        raise ValueError("TikTok upload init response must include publish_id and upload_url")
    return publish_id, upload_url


def _string_value(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _nested_string_value(data: dict[str, object], parent: str, key: str) -> str | None:
    parent_value = data.get(parent)
    if not isinstance(parent_value, dict):
        return None
    nested = {str(nested_key): nested_value for nested_key, nested_value in parent_value.items()}
    return _string_value(nested, key)


__all__ = [
    "INIT_VIDEO_UPLOAD_PATH",
    "finalize_video_upload",
    "init_video_upload",
    "upload_video_chunk",
]
