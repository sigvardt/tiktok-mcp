"""MCP tools for TikTok Display API reads and token utilities."""

from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false, reportAny=false
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyright: reportUnusedCallResult=false
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from mcp.types import ToolAnnotations
from pydantic import ValidationError

from tiktok_mcp.api.display.client import DisplayAPIClient
from tiktok_mcp.api.display.models import UserInfo, Video, VideoMetrics
from tiktok_mcp.auth.keychain import (
    app_creds_key,
    atomic_account_update,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.decorators import mark_read_only, require_writes_enabled
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountStatus, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import AccountNotFoundError, AppCredentialsNotSetError

USER_INFO_PATH = "/v2/user/info/"
VIDEO_LIST_PATH = "/v2/video/list/"
VIDEO_QUERY_PATH = "/v2/video/query/"
OAUTH_REVOKE_PATH = "/v2/oauth/revoke/"
MAX_QUERY_VIDEO_IDS = 20

logger = logging.getLogger(__name__)

DEFAULT_USER_FIELDS: tuple[str, ...] = (
    "open_id",
    "union_id",
    "display_name",
    "avatar_url",
    "avatar_url_100",
    "avatar_large_url",
    "bio_description",
    "follower_count",
    "following_count",
    "likes_count",
    "video_count",
    "is_verified",
    "profile_deep_link",
    "username",
)
DEFAULT_VIDEO_FIELDS: tuple[str, ...] = (
    "id",
    "create_time",
    "cover_image_url",
    "share_url",
    "video_description",
    "duration",
    "height",
    "width",
    "title",
    "embed_html",
    "embed_link",
    "like_count",
    "comment_count",
    "share_count",
    "view_count",
)
LIST_VIDEO_FIELDS: tuple[str, ...] = (
    "id",
    "title",
)
QUERY_VIDEO_FIELDS: tuple[str, ...] = DEFAULT_VIDEO_FIELDS
VIDEO_METRICS_FIELDS: tuple[str, ...] = (
    "id",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "embed_html",
    "embed_link",
)
_VIDEO_QUERY_ENRICHMENT_FIELD_NAMES = frozenset(QUERY_VIDEO_FIELDS) - frozenset(LIST_VIDEO_FIELDS)
USER_FIELD_SCOPES: Mapping[str, str] = {
    "bio_description": "user.info.profile",
    "is_verified": "user.info.profile",
    "profile_deep_link": "user.info.profile",
    "username": "user.info.profile",
    "follower_count": "user.info.stats",
    "following_count": "user.info.stats",
    "likes_count": "user.info.stats",
    "video_count": "user.info.stats",
}


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def display_get_user_info(
    alias: str,
    fields: list[str] | None = None,
    sandbox: bool | None = None,
) -> dict[str, object]:
    """Get Display API user profile fields for an authorized account."""
    account, client = await _build_display_client(alias, sandbox=sandbox)
    request_fields = _allowed_user_fields(account, fields)
    try:
        data = await _request_json_object(
            client,
            "GET",
            USER_INFO_PATH,
            params=_fields_params(request_fields),
        )
    finally:
        await client.aclose()

    user_payload = _nested_object(data, "user")
    user_info = UserInfo.model_validate(_scope_gated_user_payload(account, user_payload))
    return cast(dict[str, object], user_info.model_dump(mode="json"))


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def display_list_videos(
    alias: str,
    cursor: int | None = None,
    max_count: int = 20,
    fields: list[str] | None = None,
    sandbox: bool | None = None,
    enrich: bool = True,
) -> dict[str, object]:
    """List videos and auto-fill rich fields via video/query when requested.

    TikTok's video/list surface is reliable for LIST_VIDEO_FIELDS. Rich Video Object
    fields such as duration, cover_image_url, share_url, create_time, and engagement
    counts are fetched in one batched video/query call when enrich is true. Pass
    enrich=False to return the raw video/list shape without the extra request.
    """
    _account, client = await _build_display_client(alias, sandbox=sandbox)
    request_fields = _field_list(fields, DEFAULT_VIDEO_FIELDS)
    list_fields = _list_video_fields(request_fields, enrich=enrich)
    body: dict[str, object] = {"max_count": max_count}
    if cursor is not None:
        body["cursor"] = cursor

    try:
        data = await _request_json_object(
            client,
            "POST",
            VIDEO_LIST_PATH,
            params=_fields_params(list_fields),
            json_body=body,
        )
        videos = [_dump_video(video_payload) for video_payload in _object_list(data, "videos")]
        videos = await _enrich_list_videos(client, videos, request_fields, enrich=enrich)
    finally:
        await client.aclose()

    return {
        "videos": videos,
        "cursor": _required_int(data, "cursor"),
        "has_more": _required_bool(data, "has_more"),
    }


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def display_query_videos(
    alias: str,
    video_ids: list[str],
    fields: list[str] | None = None,
    sandbox: bool | None = None,
) -> list[dict[str, object]]:
    """Query up to 20 Display API videos by video ID."""
    return await _query_videos(alias, video_ids, fields, sandbox=sandbox)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def display_get_video_metrics(
    alias: str,
    video_id: str,
    sandbox: bool | None = None,
) -> dict[str, object]:
    """Get Display API metrics for one video."""
    videos = await _query_videos(alias, [video_id], list(VIDEO_METRICS_FIELDS), sandbox=sandbox)
    if not videos:
        raise ValueError(f"Display API returned no video for video_id={video_id!r}")
    metrics = VideoMetrics.model_validate(videos[0])
    return cast(dict[str, object], metrics.model_dump(mode="json"))


async def _query_videos(
    alias: str,
    video_ids: list[str],
    fields: list[str] | None = None,
    *,
    sandbox: bool | None = None,
) -> list[dict[str, object]]:
    if len(video_ids) > MAX_QUERY_VIDEO_IDS:
        raise ValueError("display_query_videos accepts at most 20 video_ids per call")

    _account, client = await _build_display_client(alias, sandbox=sandbox)
    request_fields = _field_list(fields, DEFAULT_VIDEO_FIELDS)
    try:
        video_payloads = await _query_video_payloads(client, video_ids, request_fields)
    finally:
        await client.aclose()

    return [_dump_video(video_payload) for video_payload in video_payloads]


async def _enrich_list_videos(
    client: DisplayAPIClient,
    videos: list[dict[str, object]],
    request_fields: Sequence[str],
    *,
    enrich: bool,
) -> list[dict[str, object]]:
    query_fields = _query_enrichment_fields(request_fields, enrich=enrich)
    if not query_fields or not videos:
        return videos

    video_ids = [str(video["id"]) for video in videos]
    logger.info(
        "%s alias=%s video_count=%s fields=%s",
        "Auto-enriching Display video/list response via video/query",
        client.account.alias,
        len(video_ids),
        ",".join(query_fields),
    )
    query_payloads = await _query_video_payloads(client, video_ids, query_fields)
    enrichment_by_id = _video_enrichment_by_id(query_payloads, query_fields)

    enriched_videos: list[dict[str, object]] = []
    for video in videos:
        enriched_video = dict(video)
        enriched_video.update(enrichment_by_id.get(str(video["id"]), {}))
        enriched_videos.append(enriched_video)
    return enriched_videos


async def _query_video_payloads(
    client: DisplayAPIClient,
    video_ids: Sequence[str],
    fields: Sequence[str],
) -> list[dict[str, object]]:
    video_payloads: list[dict[str, object]] = []
    for batch in _chunks(video_ids, MAX_QUERY_VIDEO_IDS):
        data = await _request_json_object(
            client,
            "POST",
            VIDEO_QUERY_PATH,
            params=_fields_params(fields),
            json_body={"filters": {"video_ids": list(batch)}},
        )
        video_payloads.extend(_object_list(data, "videos"))
    return video_payloads


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("display")
async def display_refresh_token(alias: str) -> dict[str, object]:
    """Force a Display OAuth refresh-token rotation for a stored account."""
    _account, client = await _build_display_client(alias)
    try:
        stored_account, stored_tokens = await client._load_account_record()
        refreshed_tokens = await client._refresh_tokens(stored_account, stored_tokens)
    finally:
        await client.aclose()

    return {
        "alias": alias,
        "refreshed": True,
        "access_token_expires_at": _optional_datetime_to_json(
            refreshed_tokens.access_token_expires_at
        ),
        "refresh_token_expires_at": _optional_datetime_to_json(
            refreshed_tokens.refresh_token_expires_at,
        ),
    }


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("display")
async def display_revoke_token(alias: str) -> dict[str, object]:
    """Revoke a Display OAuth token and mark the account revoked without deleting it."""
    _account, client = await _build_display_client(alias)
    try:
        _ = await _request_json_object(client, "POST", OAUTH_REVOKE_PATH, json_body={})
        account, tokens = await client._load_account_record()
        revoked_account = account.model_copy(update={"status": AccountStatus.REVOKED})
        backend = await get_backend()
        await atomic_account_update(
            backend,
            revoked_account.api_type,
            revoked_account.sandbox,
            revoked_account.alias,
            revoked_account,
            tokens,
        )
    finally:
        await client.aclose()

    return {"alias": alias, "revoked": True, "status": AccountStatus.REVOKED.value}


async def _build_display_client(
    alias: str,
    *,
    sandbox: bool | None = None,
) -> tuple[Account, DisplayAPIClient]:
    account = await _load_display_account(alias, sandbox=sandbox)
    credentials = await _load_display_app_credentials(account.sandbox)
    return account, DisplayAPIClient(account, credentials)


async def _load_display_account(alias: str, *, sandbox: bool | None = None) -> Account:
    backend = await get_backend()
    for key in await backend.list_keys("tiktok-mcp::display::"):
        if not key.endswith(f"::account::{alias}"):
            continue
        raw_record = await backend.get(key)
        if raw_record is None:
            continue
        account, _tokens = deserialize_account_record(raw_record)
        if account.api_type is ApiType.DISPLAY and (sandbox is None or account.sandbox is sandbox):
            return account
    raise AccountNotFoundError(alias, api_type=ApiType.DISPLAY.value)


async def _load_display_app_credentials(sandbox: bool) -> AppCredentials:
    backend = await get_backend()
    raw_credentials = await backend.get(app_creds_key(ApiType.DISPLAY, sandbox))
    if raw_credentials is None:
        raise AppCredentialsNotSetError(ApiType.DISPLAY.value, sandbox)

    try:
        payload = cast(object, json.loads(raw_credentials))
    except json.JSONDecodeError as exc:
        raise AppCredentialsNotSetError(ApiType.DISPLAY.value, sandbox) from exc
    if not isinstance(payload, dict):
        raise AppCredentialsNotSetError(ApiType.DISPLAY.value, sandbox)

    credentials_payload = _credentials_payload({str(key): value for key, value in payload.items()})
    try:
        return AppCredentials.model_validate(credentials_payload)
    except ValidationError as exc:
        raise AppCredentialsNotSetError(ApiType.DISPLAY.value, sandbox) from exc


async def _request_json_object(
    client: DisplayAPIClient,
    method: str,
    path: str,
    *,
    params: Mapping[str, str | int | float | bool | None] | None = None,
    json_body: Mapping[str, object] | None = None,
) -> dict[str, object]:
    data = await client.request(
        method,
        path,
        params=params,
        json=dict(json_body) if json_body is not None else None,
    )
    if not isinstance(data, dict):
        raise ValueError(f"Display API response data for {path} must be a JSON object")
    return {str(key): value for key, value in data.items()}


def _credentials_payload(payload: Mapping[str, object]) -> dict[str, object]:
    nested_credentials = payload.get("credentials")
    if isinstance(nested_credentials, dict):
        source = {str(key): value for key, value in nested_credentials.items()}
    else:
        source = dict(payload)
    return {
        key: source[key]
        for key in {"api_type", "sandbox", "client_id", "client_secret", "created_at"}
        if key in source
    }


def _allowed_user_fields(account: Account, fields: Sequence[str] | None) -> list[str]:
    requested_fields = _field_list(fields, DEFAULT_USER_FIELDS)
    granted_scopes = set(account.scopes)
    return [
        field
        for field in requested_fields
        if USER_FIELD_SCOPES.get(field) is None or USER_FIELD_SCOPES[field] in granted_scopes
    ]


def _scope_gated_user_payload(
    account: Account,
    payload: Mapping[str, object],
) -> dict[str, object]:
    gated_payload = dict(payload)
    granted_scopes = set(account.scopes)
    for field, required_scope in USER_FIELD_SCOPES.items():
        if required_scope not in granted_scopes:
            gated_payload.pop(field, None)
    return gated_payload


def _field_list(fields: Sequence[str] | None, defaults: Sequence[str]) -> list[str]:
    if fields is None:
        return list(defaults)
    return list(fields)


def _list_video_fields(request_fields: Sequence[str], *, enrich: bool) -> list[str]:
    if not enrich:
        return list(request_fields)
    return _with_required_video_id(
        [field for field in request_fields if field not in _VIDEO_QUERY_ENRICHMENT_FIELD_NAMES]
    )


def _query_enrichment_fields(request_fields: Sequence[str], *, enrich: bool) -> list[str]:
    if not enrich:
        return []
    enrichment_fields = [
        field for field in request_fields if field in _VIDEO_QUERY_ENRICHMENT_FIELD_NAMES
    ]
    if not enrichment_fields:
        return []
    return _with_required_video_id(enrichment_fields)


def _with_required_video_id(fields: Sequence[str]) -> list[str]:
    request_fields = list(fields)
    if "id" in request_fields:
        return request_fields
    return ["id", *request_fields]


def _video_enrichment_by_id(
    video_payloads: Sequence[Mapping[str, object]],
    fields: Sequence[str],
) -> dict[str, dict[str, object]]:
    enrichment_by_id: dict[str, dict[str, object]] = {}
    for video_payload in video_payloads:
        dumped_video = _dump_video(video_payload)
        video_id = str(dumped_video["id"])
        enrichment_by_id[video_id] = {
            field: dumped_video[field] for field in fields if field in dumped_video
        }
    return enrichment_by_id


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[start_index : start_index + size] for start_index in range(0, len(items), size)]


def _fields_params(fields: Sequence[str]) -> dict[str, str]:
    return {"fields": ",".join(fields)}


def _nested_object(payload: Mapping[str, object], key: str) -> dict[str, object]:
    nested = payload.get(key)
    if isinstance(nested, dict):
        return {str(item_key): item_value for item_key, item_value in nested.items()}
    return dict(payload)


def _object_list(payload: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"Display API response field {key!r} must be a list")
    objects: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"Display API response field {key!r} must contain objects")
        objects.append({str(item_key): item_value for item_key, item_value in item.items()})
    return objects


def _dump_video(video_payload: Mapping[str, object]) -> dict[str, object]:
    video = Video.model_validate(video_payload)
    return cast(dict[str, object], video.model_dump(mode="json"))


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Display API response field {key!r} must be an integer")
    return value


def _required_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Display API response field {key!r} must be a boolean")
    return value


def _datetime_to_json(value: datetime) -> str:
    return value.isoformat()


def _optional_datetime_to_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _datetime_to_json(value)


__all__ = [
    "DEFAULT_USER_FIELDS",
    "DEFAULT_VIDEO_FIELDS",
    "LIST_VIDEO_FIELDS",
    "MAX_QUERY_VIDEO_IDS",
    "QUERY_VIDEO_FIELDS",
    "VIDEO_METRICS_FIELDS",
    "display_get_user_info",
    "display_get_video_metrics",
    "display_list_videos",
    "display_query_videos",
    "display_refresh_token",
    "display_revoke_token",
]
