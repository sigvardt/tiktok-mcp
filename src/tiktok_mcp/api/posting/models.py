from __future__ import annotations

from enum import Enum
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, model_validator


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

    creator_avatar_url: str
    creator_username: str | None = None
    creator_nickname: str | None = None
    privacy_level_options: list[str] | None = None
    comment_disabled: bool | None = None
    duet_disabled: bool | None = None
    stitch_disabled: bool | None = None
    max_video_post_duration_sec: int | None = None

    @model_validator(mode="after")
    def validate_creator_identity(self) -> Self:
        if self.creator_username is not None or self.creator_nickname is not None:
            return self
        raise ValueError("creator info requires creator_username or creator_nickname")


__all__ = ["CreatorInfo", "PostPublishStatus", "PostStatus"]
