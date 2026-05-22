from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class CommentAuthor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    open_id: str
    display_name: str | None = None
    avatar_url: str | None = None


class Comment(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    comment_id: str
    parent_comment_id: str | None = None
    author: CommentAuthor
    text: str
    like_count: int
    reply_count: int
    create_time: int
    is_top_pinned: bool
    is_hidden_by_owner: bool
    is_deleted_by_author: bool


__all__ = ["Comment", "CommentAuthor"]
