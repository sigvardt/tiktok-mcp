from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import ClassVar, Literal, Protocol, Self, cast

from mcp.types import ToolAnnotations
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from tiktok_mcp.api.posting import PostingAPIClient
from tiktok_mcp.api.posting.client import POST_STATUS_PATH
from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.decorators import mark_read_only, require_writes_enabled
from tiktok_mcp.server import app

INBOX_VIDEO_INIT_PATH = "/v2/post/publish/inbox/video/init/"
DIRECT_VIDEO_INIT_PATH = "/v2/post/publish/video/init/"
PHOTO_INIT_PATH = "/v2/post/publish/content/init/"
CANCEL_PUBLISH_PATH = "/v2/post/publish/cancel/"
MAX_PHOTO_URLS = 35
PUBLISH_ALIAS_TTL = timedelta(hours=1)
UNKNOWN_PUBLISH_ID_OR_EXPIRED_MESSAGE = (
    "TikTok returned HTTP 400 for this publish_id. The id may be unknown, expired, "
    "or malformed. Verify the publish_id from a prior upload tool."
)

PrivacyLevel = Literal[
    "MUTUAL_FOLLOW_FRIENDS",
    "SELF_ONLY",
    "PUBLIC_TO_EVERYONE",
    "FOLLOWER_OF_CREATOR",
]
JsonObject = dict[str, object]

PENDING_PUBLISH_STATUSES = frozenset(
    {
        "FETCH_IN_PROGRESS",
        "PROCESSING_DOWNLOAD",
        "PROCESSING_UPLOAD",
        "PROCESSING_PUBLISH",
    }
)


@dataclass(frozen=True)
class PublishAlias:
    alias: str
    expires_at: datetime


class PostingRequestClient(Protocol):
    async def request(
        self,
        alias: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, object],
    ) -> dict[str, object]: ...


class PostingClientContext(Protocol):
    async def __aenter__(self) -> PostingRequestClient: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


_PUBLISH_ALIASES: dict[str, PublishAlias] = {}
_PUBLISH_ALIAS_LOCK = asyncio.Lock()


class DirectPostInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    privacy_level: PrivacyLevel
    description: str | None = Field(default=None, min_length=1)
    disable_duet: bool | None = None
    disable_comment: bool | None = None
    disable_stitch: bool | None = None
    video_cover_timestamp_ms: int | None = Field(default=None, ge=0)
    auto_add_music: bool | None = None
    brand_content_toggle: bool | None = None
    brand_organic_toggle: bool | None = None
    is_aigc: bool | None = None


class VideoFromUrlParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    video_url: HttpUrl
    publish_immediately: bool = False
    post_info: DirectPostInfo | None = None

    @field_validator("video_url")
    @classmethod
    def validate_video_url_https(cls, value: HttpUrl) -> HttpUrl:
        return _https_url(value)

    @model_validator(mode="after")
    def validate_post_info_for_direct_post(self) -> Self:
        if self.publish_immediately and self.post_info is None:
            raise ValueError(
                "publish_immediately=True requires post_info with title and privacy_level"
            )
        return self


class PhotoFromUrlsParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    photo_urls: list[HttpUrl] = Field(min_length=1, max_length=MAX_PHOTO_URLS)
    publish_immediately: bool = False
    post_info: DirectPostInfo | None = None

    @field_validator("photo_urls")
    @classmethod
    def validate_photo_urls_https(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        return [_https_url(url) for url in value]

    @model_validator(mode="after")
    def validate_post_info_for_direct_post(self) -> Self:
        if self.publish_immediately and self.post_info is None:
            raise ValueError(
                "publish_immediately=True requires post_info with title and privacy_level"
            )
        return self


class PublishIdParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    publish_id: str = Field(min_length=1)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def upload_video_from_url(
    alias: str,
    video_url: str,
    publish_immediately: bool = False,
    post_info: dict[str, object] | None = None,
) -> JsonObject:
    """Upload a TikTok video by letting TikTok pull a verified HTTPS URL.

    Draft inbox is the default. Direct Post is used only when
    publish_immediately=True and post_info validates with title and privacy_level.
    """
    try:
        params = VideoFromUrlParams.model_validate(
            {
                "alias": alias,
                "video_url": video_url,
                "publish_immediately": publish_immediately,
                "post_info": post_info,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    path = DIRECT_VIDEO_INIT_PATH if params.publish_immediately else INBOX_VIDEO_INIT_PATH
    body: JsonObject = {
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": _url_string(params.video_url),
        }
    }
    if params.publish_immediately and params.post_info is not None:
        body["post_info"] = _model_payload(params.post_info)

    payload = await _post_json(params.alias, path, body)
    await _remember_publish_alias(_optional_string(payload.get("publish_id")), params.alias)
    return payload


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def upload_photo_from_urls(
    alias: str,
    photo_urls: list[str],
    publish_immediately: bool = False,
    post_info: dict[str, object] | None = None,
) -> JsonObject:
    """Upload up to 35 static photo URLs as one TikTok carousel/photo post.

    Multi-photo upload (this tool) ≠ Slideshow (v0.2 feature): this creates a
    static carousel post, not auto-advancing playback with music.
    """
    try:
        params = PhotoFromUrlsParams.model_validate(
            {
                "alias": alias,
                "photo_urls": photo_urls,
                "publish_immediately": publish_immediately,
                "post_info": post_info,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    body: JsonObject = {
        "media_type": "PHOTO",
        "post_mode": "DIRECT_POST" if params.publish_immediately else "MEDIA_UPLOAD",
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_images": {
                "image_urls": [_url_string(url) for url in params.photo_urls],
            },
        },
    }
    if params.publish_immediately and params.post_info is not None:
        body["post_info"] = _model_payload(params.post_info)

    payload = await _post_json(params.alias, PHOTO_INIT_PATH, body)
    await _remember_publish_alias(_optional_string(payload.get("publish_id")), params.alias)
    return payload


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@mark_read_only
@require_writes_enabled("posting")
async def get_publish_status(publish_id: str) -> JsonObject:
    """Fetch the current Content Posting status once; this is not a polling loop."""
    try:
        params = PublishIdParams.model_validate({"publish_id": publish_id})
    except ValidationError as exc:
        return _validation_error(exc)

    alias = await _publish_alias(params.publish_id)
    if alias is None:
        return _unknown_publish_id(params.publish_id)
    try:
        return await _fetch_publish_status(alias, params.publish_id)
    except SanitizedHttpxError as exc:
        if _is_unknown_publish_id_400(exc):
            return _unknown_publish_id_envelope(
                tool="get_publish_status",
                publish_id=params.publish_id,
                request_id=exc.request_id,
            )
        raise


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("posting")
async def cancel_publish(publish_id: str) -> JsonObject:
    """Cancel a pending Content Posting publish if its latest single status fetch is pending."""
    try:
        params = PublishIdParams.model_validate({"publish_id": publish_id})
    except ValidationError as exc:
        return _validation_error(exc)

    alias = await _publish_alias(params.publish_id)
    if alias is None:
        return _unknown_publish_id(params.publish_id)

    status_payload = await _fetch_publish_status(alias, params.publish_id)
    status = _optional_string(status_payload.get("status"))
    if status not in PENDING_PUBLISH_STATUSES:
        return {
            "publish_id": params.publish_id,
            "cancelled": False,
            "already_terminal": True,
            "status": status,
        }

    cancel_payload = await _post_json(alias, CANCEL_PUBLISH_PATH, {"publish_id": params.publish_id})
    return {
        "publish_id": params.publish_id,
        "cancelled": True,
        "status": status,
        "cancel_response": cancel_payload,
    }


async def _post_json(alias: str, path: str, body: JsonObject) -> JsonObject:
    async with _build_posting_client() as client:
        payload = await client.request(alias, "POST", path, json_body=body)
    return _raw_payload(payload)


async def _fetch_publish_status(alias: str, publish_id: str) -> JsonObject:
    return await _post_json(alias, POST_STATUS_PATH, {"publish_id": publish_id})


def _build_posting_client() -> PostingClientContext:
    return cast(PostingClientContext, cast(object, PostingAPIClient()))


def _https_url(value: HttpUrl) -> HttpUrl:
    if value.scheme != "https":
        raise ValueError("video_url and photo_urls must use HTTPS URLs")
    return value


def _url_string(value: HttpUrl) -> str:
    return str(value)


def _model_payload(model: BaseModel) -> JsonObject:
    return cast(JsonObject, model.model_dump(mode="json", exclude_none=True))


def _raw_payload(payload: object) -> JsonObject:
    if isinstance(payload, dict):
        payload_mapping = cast(dict[object, object], payload)
        return {str(key): value for key, value in payload_mapping.items()}
    return {"data": payload}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _validation_error(exc: ValidationError) -> JsonObject:
    return {
        "error": "validation_error",
        "message": str(exc),
        "details": exc.errors(),
    }


async def _remember_publish_alias(publish_id: str | None, alias: str) -> None:
    if publish_id is None:
        return
    now = datetime.now(UTC)
    async with _PUBLISH_ALIAS_LOCK:
        _drop_expired_publish_aliases(now)
        _PUBLISH_ALIASES[publish_id] = PublishAlias(alias, now + PUBLISH_ALIAS_TTL)


async def _publish_alias(publish_id: str) -> str | None:
    now = datetime.now(UTC)
    async with _PUBLISH_ALIAS_LOCK:
        _drop_expired_publish_aliases(now)
        record = _PUBLISH_ALIASES.get(publish_id)
        if record is None:
            return None
        return record.alias


def _drop_expired_publish_aliases(now: datetime) -> None:
    expired_publish_ids = [
        publish_id
        for publish_id, record in _PUBLISH_ALIASES.items()
        if record.expires_at <= now
    ]
    for publish_id in expired_publish_ids:
        _ = _PUBLISH_ALIASES.pop(publish_id, None)


def _unknown_publish_id(publish_id: str) -> JsonObject:
    return {
        "error": "publish_alias_not_found",
        "message": (
            "No recent alias is cached for publish_id; use "
            "posting_get_post_status(alias, publish_id) for older publishes."
        ),
        "publish_id": publish_id,
    }


def _is_unknown_publish_id_400(exc: SanitizedHttpxError) -> bool:
    return exc.status == 400 and exc.url_path == POST_STATUS_PATH


def _unknown_publish_id_envelope(
    *,
    tool: str,
    publish_id: str,
    request_id: str | None,
) -> JsonObject:
    return {
        "error": "unknown_publish_id_or_expired",
        "tool": tool,
        "publish_id": publish_id,
        "message": UNKNOWN_PUBLISH_ID_OR_EXPIRED_MESSAGE,
        "request_id": request_id,
    }


__all__ = [
    "CANCEL_PUBLISH_PATH",
    "DIRECT_VIDEO_INIT_PATH",
    "INBOX_VIDEO_INIT_PATH",
    "PHOTO_INIT_PATH",
    "cancel_publish",
    "get_publish_status",
    "upload_photo_from_urls",
    "upload_video_from_url",
]
