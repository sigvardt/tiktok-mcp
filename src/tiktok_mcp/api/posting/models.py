from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class PostPublishStatus(str, Enum):  # noqa: UP042
    PROCESSING_DOWNLOAD = "PROCESSING_DOWNLOAD"
    PROCESSING_UPLOAD = "PROCESSING_UPLOAD"
    PROCESSING_PUBLISH = "PROCESSING_PUBLISH"
    PUBLISH_COMPLETE = "PUBLISH_COMPLETE"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class PostStatus(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    status: PostPublishStatus
    uploaded_bytes: int | None = None
    video_seconds: int | None = None
    publicaly_available_post_id: str | None = None
    fail_reason: str | None = None


class CreatorInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    privacy_level_options: list[str]
    max_video_post_duration_sec: int
    comment_disabled_supported: bool
    creator_avatar_url: str
    creator_username: str
    creator_nickname: str


__all__ = ["CreatorInfo", "PostPublishStatus", "PostStatus"]
