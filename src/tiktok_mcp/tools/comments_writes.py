# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
from __future__ import annotations

import logging
import os
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal, Protocol, cast

from mcp.types import ToolAnnotations

from tiktok_mcp.decorators import require_writes_enabled
from tiktok_mcp.server import app
from tiktok_mcp.tools.comments_read import COMMENT_BODY_LOG_ENV, _build_comments_client

COMMENT_REPLY_CREATE_PATH = "/open_api/v1.3/business/comment/reply/create/"
COMMENT_PIN_PATH = "/open_api/v1.3/business/comment/pin/"
COMMENT_HIDE_PATH = "/open_api/v1.3/business/comment/hide/"
COMMENT_REPLY_DELETE_PATH = "/open_api/v1.3/business/comment/reply/delete/"
REPLY_TEXT_MAX_LENGTH = 150

CommentModerationAction = Literal["PIN", "UNPIN", "HIDE", "UNHIDE"]

logger = logging.getLogger(__name__)


class CommentWriteBusinessClient(Protocol):
    async def post(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        json: object | None = None,
        idempotent: bool = False,
    ) -> dict[str, object]: ...


class CommentWriteClientContext(Protocol):
    async def __aenter__(self) -> CommentWriteBusinessClient: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("comments")
async def post_comment_reply(
    alias: str,
    business_id: str,
    account_id: str,
    comment_id: str,
    reply_text: str,
) -> dict[str, object]:
    normalized_reply_text = _validated_reply_text(reply_text)
    await _validate_comment_belongs_to_account(account_id=account_id, comment_id=comment_id)
    payload = await _post_comment_write(
        alias,
        COMMENT_REPLY_CREATE_PATH,
        json={
            "business_id": business_id,
            "account_id": account_id,
            "comment_id": comment_id,
            "comment_text": normalized_reply_text,
        },
    )
    request_id = _request_id_from_payload(payload)
    reply_id = _first_string(payload.get("reply_id"), payload.get("comment_id"))
    _log_reply_body(normalized_reply_text)
    _log_action(
        logging.INFO,
        action="REPLY",
        business_id=business_id,
        account_id=account_id,
        comment_id=comment_id,
        request_id=request_id,
    )
    return {
        "replied": True,
        "action": "REPLY",
        "business_id": business_id,
        "account_id": account_id,
        "comment_id": comment_id,
        "reply_id": reply_id,
        "request_id": request_id,
    }


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("comments")
async def pin_comment(
    alias: str,
    business_id: str,
    account_id: str,
    comment_id: str,
) -> dict[str, object]:
    return await _post_moderation_action(
        alias,
        business_id=business_id,
        account_id=account_id,
        comment_id=comment_id,
        path=COMMENT_PIN_PATH,
        action="PIN",
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("comments")
async def unpin_comment(
    alias: str,
    business_id: str,
    account_id: str,
    comment_id: str,
) -> dict[str, object]:
    return await _post_moderation_action(
        alias,
        business_id=business_id,
        account_id=account_id,
        comment_id=comment_id,
        path=COMMENT_PIN_PATH,
        action="UNPIN",
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("comments")
async def hide_comment(
    alias: str,
    business_id: str,
    account_id: str,
    comment_id: str,
) -> dict[str, object]:
    return await _post_moderation_action(
        alias,
        business_id=business_id,
        account_id=account_id,
        comment_id=comment_id,
        path=COMMENT_HIDE_PATH,
        action="HIDE",
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("comments")
async def unhide_comment(
    alias: str,
    business_id: str,
    account_id: str,
    comment_id: str,
) -> dict[str, object]:
    return await _post_moderation_action(
        alias,
        business_id=business_id,
        account_id=account_id,
        comment_id=comment_id,
        path=COMMENT_HIDE_PATH,
        action="UNHIDE",
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("comments")
async def delete_own_reply(
    alias: str,
    business_id: str,
    account_id: str,
    comment_id: str,
) -> dict[str, object]:
    await _validate_comment_belongs_to_account(account_id=account_id, comment_id=comment_id)
    payload = await _post_comment_write(
        alias,
        COMMENT_REPLY_DELETE_PATH,
        json={
            "business_id": business_id,
            "account_id": account_id,
            "comment_id": comment_id,
        },
    )
    request_id = _request_id_from_payload(payload)
    _log_action(
        logging.WARNING,
        action="DELETE_OWN_REPLY",
        business_id=business_id,
        account_id=account_id,
        comment_id=comment_id,
        request_id=request_id,
    )
    return {
        "deleted": True,
        "business_id": business_id,
        "account_id": account_id,
        "comment_id": comment_id,
        "deleted_at": _utc_now_iso(),
        "request_id": request_id,
    }


async def _post_moderation_action(
    alias: str,
    *,
    business_id: str,
    account_id: str,
    comment_id: str,
    path: str,
    action: CommentModerationAction,
) -> dict[str, object]:
    await _validate_comment_belongs_to_account(account_id=account_id, comment_id=comment_id)
    payload = await _post_comment_write(
        alias,
        path,
        json={
            "business_id": business_id,
            "account_id": account_id,
            "comment_id": comment_id,
            "action": action,
        },
    )
    request_id = _request_id_from_payload(payload)
    _log_action(
        logging.INFO,
        action=action,
        business_id=business_id,
        account_id=account_id,
        comment_id=comment_id,
        request_id=request_id,
    )
    return {
        "action": action,
        "business_id": business_id,
        "account_id": account_id,
        "comment_id": comment_id,
        "request_id": request_id,
    }


async def _post_comment_write(
    alias: str,
    path: str,
    *,
    json: Mapping[str, object],
) -> dict[str, object]:
    async with _build_comments_write_client(alias) as client:
        return await client.post(path, json=dict(json))


def _build_comments_write_client(alias: str) -> CommentWriteClientContext:
    return cast(CommentWriteClientContext, cast(object, _build_comments_client(alias)))


async def _validate_comment_belongs_to_account(*, account_id: str, comment_id: str) -> None:
    _ = (account_id, comment_id)


def _validated_reply_text(reply_text: str) -> str:
    if len(reply_text) > REPLY_TEXT_MAX_LENGTH:
        raise ValueError("reply_text must be at most 150 characters")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in reply_text):
        raise ValueError("reply_text must not contain surrogate code points")
    return unicodedata.normalize("NFC", reply_text)


def _log_action(
    level: int,
    *,
    action: str,
    business_id: str,
    account_id: str,
    comment_id: str,
    request_id: str | None,
) -> None:
    logger.log(
        level,
        "comment moderation write",
        extra={
            "action": action,
            "business_id": business_id,
            "account_id": account_id,
            "comment_id": comment_id,
            "request_id": request_id,
        },
    )


def _log_reply_body(reply_text: str) -> None:
    if logger.isEnabledFor(logging.DEBUG) and os.environ.get(COMMENT_BODY_LOG_ENV) == "1":
        logger.debug("comment reply body text=%s", reply_text)
    else:
        logger.debug("comment reply body redacted")


def _request_id_from_payload(payload: Mapping[str, object]) -> str | None:
    return _first_string(payload.get("request_id"), payload.get("log_id"))


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "COMMENT_HIDE_PATH",
    "COMMENT_PIN_PATH",
    "COMMENT_REPLY_CREATE_PATH",
    "COMMENT_REPLY_DELETE_PATH",
    "REPLY_TEXT_MAX_LENGTH",
    "delete_own_reply",
    "hide_comment",
    "pin_comment",
    "post_comment_reply",
    "unhide_comment",
    "unpin_comment",
]
