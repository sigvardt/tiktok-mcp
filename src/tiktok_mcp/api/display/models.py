from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class UserInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    open_id: str | None = None
    union_id: str | None = None
    avatar_url: str | None = None
    avatar_url_100: str | None = None
    avatar_large_url: str | None = None
    display_name: str | None = None
    bio_description: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    likes_count: int | None = None
    video_count: int | None = None
    is_verified: bool | None = None
    profile_deep_link: str | None = None
    username: str | None = None


class Video(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    id: str
    create_time: int | None = None
    cover_image_url: str | None = None
    share_url: str | None = None
    video_description: str | None = None
    duration: int | None = None
    height: int | None = None
    width: int | None = None
    title: str | None = None
    embed_html: str | None = None
    embed_link: str | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    view_count: int | None = None


class VideoMetrics(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    id: str
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    embed_html: str | None = None
    embed_link: str | None = None


__all__ = ["UserInfo", "Video", "VideoMetrics"]
