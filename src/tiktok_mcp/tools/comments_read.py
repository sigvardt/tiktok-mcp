from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Literal, Protocol, cast

from mcp.types import ToolAnnotations
from pydantic import ValidationError

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.api.business.comment_models import Comment
from tiktok_mcp.auth.keychain import (
    KeychainBackend,
    account_key,
    app_creds_key,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.auth.redactor import register_token as add_runtime_token
from tiktok_mcp.decorators import mark_read_only
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountStatus, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountBrokenError,
    AccountNotFoundError,
    AppCredentialsNotSetError,
)

COMMENT_LIST_PATH = "/open_api/v1.3/business/comment/list/"
COMMENT_REPLY_LIST_PATH = "/open_api/v1.3/business/comment/reply/list/"
COMMENT_BODY_LOG_ENV = "TIKTOK_MCP_LOG_COMMENT_BODIES"

QueryParams = Mapping[str, str | int | float | bool | None]

logger = logging.getLogger(__name__)


class CommentBusinessClient(Protocol):
    async def get(self, path: str, *, params: QueryParams | None = None) -> dict[str, Any]: ...


class CommentBusinessClientContext(Protocol):
    async def __aenter__(self) -> CommentBusinessClient: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def comments_list(
    alias: str,
    advertiser_id: str,
    post_id: str,
    page: int = 1,
    page_size: int = 30,
    sort_by: Literal["newest", "top"] = "newest",
) -> dict[str, object]:
    async with _build_comments_client(alias) as client:
        return await _comments_list_with_client(
            client,
            advertiser_id=advertiser_id,
            post_id=post_id,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
        )


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def comments_list_replies(
    alias: str,
    advertiser_id: str,
    post_id: str,
    comment_id: str,
    page: int = 1,
    page_size: int = 30,
) -> dict[str, object]:
    async with _build_comments_client(alias) as client:
        return await _comments_list_replies_with_client(
            client,
            advertiser_id=advertiser_id,
            post_id=post_id,
            comment_id=comment_id,
            page=page,
            page_size=page_size,
        )


async def _comments_list_with_client(
    client: CommentBusinessClient,
    *,
    advertiser_id: str,
    post_id: str,
    page: int,
    page_size: int,
    sort_by: Literal["newest", "top"],
) -> dict[str, object]:
    payload = await client.get(
        COMMENT_LIST_PATH,
        params={
            "advertiser_id": advertiser_id,
            "post_id": post_id,
            "page": page,
            "page_size": page_size,
            "sort_by": sort_by,
        },
    )
    return _comments_page_from_payload(
        payload,
        requested_page=page,
        requested_page_size=page_size,
        post_id=post_id,
        parent_comment_id=None,
        operation="comments_list",
    )


async def _comments_list_replies_with_client(
    client: CommentBusinessClient,
    *,
    advertiser_id: str,
    post_id: str,
    comment_id: str,
    page: int,
    page_size: int,
) -> dict[str, object]:
    payload = await client.get(
        COMMENT_REPLY_LIST_PATH,
        params={
            "advertiser_id": advertiser_id,
            "post_id": post_id,
            "comment_id": comment_id,
            "page": page,
            "page_size": page_size,
        },
    )
    return _comments_page_from_payload(
        payload,
        requested_page=page,
        requested_page_size=page_size,
        post_id=post_id,
        parent_comment_id=comment_id,
        operation="comments_list_replies",
    )


def _build_comments_client(alias: str) -> CommentBusinessClientContext:
    return _CommentsClientFactory(alias)


class _CommentsClientFactory:
    def __init__(self, alias: str) -> None:
        self._alias: str = alias
        self._client: BusinessAPIClient | None = None

    async def __aenter__(self) -> CommentBusinessClient:
        backend = await get_backend()
        account = await _load_business_organic_account(backend, self._alias)
        app_credentials = await _load_app_credentials(backend, account.sandbox)
        client = BusinessAPIClient(account, app_credentials, backend=backend)
        self._client = client
        return cast(CommentBusinessClient, cast(object, await client.__aenter__()))

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, traceback)


async def _load_business_organic_account(backend: KeychainBackend, alias: str) -> Account:
    key = account_key(ApiType.BUSINESS_ORGANIC, sandbox=True, alias=alias)
    raw_record = await backend.get(key)
    if raw_record is None:
        key = account_key(ApiType.BUSINESS_ORGANIC, sandbox=False, alias=alias)
        raw_record = await backend.get(key)
    if raw_record is None:
        raise AccountNotFoundError(alias, api_type=ApiType.BUSINESS_ORGANIC.value)

    account, _tokens = deserialize_account_record(raw_record)
    if account.api_type is not ApiType.BUSINESS_ORGANIC:
        raise AccountNotFoundError(alias, api_type=ApiType.BUSINESS_ORGANIC.value)
    if account.status is not AccountStatus.OK:
        raise AccountBrokenError(alias, status=account.status.value)
    return account


async def _load_app_credentials(backend: KeychainBackend, sandbox: bool) -> AppCredentials:
    raw_credentials = await backend.get(app_creds_key(ApiType.BUSINESS_ORGANIC, sandbox))
    if raw_credentials is None:
        raise AppCredentialsNotSetError(ApiType.BUSINESS_ORGANIC.value, sandbox)
    try:
        payload = cast(object, json.loads(raw_credentials))
    except json.JSONDecodeError as exc:
        raise AppCredentialsNotSetError(ApiType.BUSINESS_ORGANIC.value, sandbox) from exc
    if not isinstance(payload, dict):
        raise AppCredentialsNotSetError(ApiType.BUSINESS_ORGANIC.value, sandbox)
    try:
        credentials = AppCredentials.model_validate(
            _credentials_payload({str(key): value for key, value in payload.items()})
        )
    except ValidationError as exc:
        raise AppCredentialsNotSetError(ApiType.BUSINESS_ORGANIC.value, sandbox) from exc
    add_runtime_token(credentials.client_id.get_secret_value(), "client_id")
    add_runtime_token(credentials.client_secret.get_secret_value(), "client_secret")
    return credentials


def _credentials_payload(payload: Mapping[str, object]) -> dict[str, object]:
    nested_credentials = payload.get("credentials")
    if isinstance(nested_credentials, Mapping):
        source = {str(key): value for key, value in nested_credentials.items()}
    else:
        source = dict(payload)
    return {
        key: source[key]
        for key in {"api_type", "sandbox", "client_id", "client_secret", "created_at"}
        if key in source
    }


def _comments_page_from_payload(
    payload: Mapping[str, Any],
    *,
    requested_page: int,
    requested_page_size: int,
    post_id: str,
    parent_comment_id: str | None,
    operation: str,
) -> dict[str, object]:
    page_info = _mapping_value(payload.get("page_info")) or {}
    comments = [
        _comment_from_raw(item, parent_comment_id=parent_comment_id)
        for item in _comment_items(payload)
    ]
    page = _first_int(payload.get("page"), page_info.get("page"), default=requested_page)
    page_size = _first_int(
        payload.get("page_size"),
        page_info.get("page_size"),
        default=requested_page_size,
    )
    total = _first_int(
        payload.get("total"),
        payload.get("total_count"),
        page_info.get("total"),
        page_info.get("total_count"),
        page_info.get("total_number"),
        default=len(comments),
    )

    _log_comment_page(
        operation=operation,
        post_id=post_id,
        comments=comments,
        page=page,
        page_size=page_size,
        total=total,
    )
    return {
        "comments": [comment.model_dump(mode="json") for comment in comments],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def _comment_items(payload: Mapping[str, Any]) -> list[object]:
    for key in ("comments", "comment_list", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _comment_from_raw(raw: object, *, parent_comment_id: str | None) -> Comment:
    raw_mapping = _mapping_value(raw)
    if raw_mapping is None:
        return Comment.model_validate(raw)

    nested_author = _mapping_value(raw_mapping.get("author"))
    if nested_author is None:
        nested_author = _mapping_value(raw_mapping.get("user")) or {}

    author: dict[str, object] = {}
    open_id = _first_string(raw_mapping.get("open_id"), nested_author.get("open_id"))
    if open_id is not None:
        author["open_id"] = open_id
    display_name = _first_string(
        raw_mapping.get("display_name"),
        nested_author.get("display_name"),
        nested_author.get("username"),
    )
    if display_name is not None:
        author["display_name"] = display_name
    avatar_url = _first_string(raw_mapping.get("avatar_url"), nested_author.get("avatar_url"))
    if avatar_url is not None:
        author["avatar_url"] = avatar_url

    normalized: dict[str, object] = {
        "parent_comment_id": _first_string(raw_mapping.get("parent_comment_id"))
        or parent_comment_id,
        "author": author,
        "like_count": _first_int(raw_mapping.get("like_count"), default=0),
        "reply_count": _first_int(raw_mapping.get("reply_count"), default=0),
        "is_top_pinned": _first_bool(
            raw_mapping.get("is_top_pinned"),
            raw_mapping.get("is_pinned"),
            default=False,
        ),
        "is_hidden_by_owner": _first_bool(
            raw_mapping.get("is_hidden_by_owner"),
            raw_mapping.get("is_hidden"),
            default=False,
        ),
        "is_deleted_by_author": _first_bool(
            raw_mapping.get("is_deleted_by_author"),
            raw_mapping.get("is_deleted"),
            default=False,
        ),
    }

    for key in ("comment_id", "create_time"):
        if key in raw_mapping:
            normalized[key] = raw_mapping[key]
    text = _first_string(raw_mapping.get("text"), raw_mapping.get("comment_text"))
    if text is not None:
        normalized["text"] = text
    return Comment.model_validate(normalized)


def _log_comment_page(
    *,
    operation: str,
    post_id: str,
    comments: list[Comment],
    page: int,
    page_size: int,
    total: int,
) -> None:
    comment_ids = [comment.comment_id for comment in comments]
    logger.info(
        "%s fetched comment metadata post_id=%s page=%s page_size=%s total=%s comment_ids=%s",
        operation,
        post_id,
        page,
        page_size,
        total,
        comment_ids,
    )
    if _comment_body_logging_enabled():
        logger.debug(
            "%s comment bodies: %s",
            operation,
            [comment.model_dump(mode="json") for comment in comments],
        )
    else:
        logger.debug("%s comment bodies redacted", operation)


def _comment_body_logging_enabled() -> bool:
    return logger.isEnabledFor(logging.DEBUG) and os.environ.get(COMMENT_BODY_LOG_ENV) == "1"


def _mapping_value(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _first_int(*values: object, default: int) -> int:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
    return default


def _first_bool(*values: object, default: bool) -> bool:
    for value in values:
        if isinstance(value, bool):
            return value
    return default


__all__ = ["comments_list", "comments_list_replies"]
