from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.auth import state as oauth_state_store
from tiktok_mcp.auth.keychain import (
    KeychainBackend,
    account_key,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.tools.accounts import add_account, complete_account_login
from tiktok_mcp.tools.comments_read import _load_app_credentials as load_app_credentials
from tiktok_mcp.tools.comments_read import comments_list, comments_list_replies
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.oauth import OAuthInProgress

VIDEO_LIST_PATH = "/open_api/v1.3/business/video/list/"
VIDEO_LIST_FIELDS = ("item_id", "comments", "create_time", "media_type", "share_url")
DEFAULT_ALIAS = "comments-live-e2e"
TOKEN_FRESHNESS_MARGIN = timedelta(minutes=5)
VIDEO_LIST_MAX_PAGES = 20


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only live smoke test for the Business Organic comment read tools."
    )
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument("--video-id", default=os.environ.get("TIKTOK_MCP_COMMENTS_VIDEO_ID"))
    parser.add_argument(
        "--oauth",
        action="store_true",
        help="Create/replace the local account by running the manual OAuth handoff first.",
    )
    parser.add_argument(
        "--recover-redirect",
        action="store_true",
        help="Read a pasted redirect URL from stdin and recover a stopped manual OAuth handoff.",
    )
    args = parser.parse_args()

    result = asyncio.run(
        run(
            alias=args.alias,
            video_id=args.video_id,
            oauth=args.oauth,
            recover_redirect=args.recover_redirect,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "ok" else 2


async def run(
    *,
    alias: str,
    video_id: str | None,
    oauth: bool,
    recover_redirect: bool,
) -> dict[str, object]:
    backend = await get_backend()
    if recover_redirect:
        recover_result = await _recover_manual_oauth(alias)
        if "error" in recover_result:
            return {"status": "oauth_failed", "oauth": recover_result}

    if oauth:
        oauth_result = await _run_manual_oauth(alias)
        if "error" in oauth_result:
            return {"status": "oauth_failed", "oauth": oauth_result}

    loaded = await _load_account_with_tokens(backend, alias)
    if loaded is None:
        return {
            "status": "needs_oauth",
            "alias": alias,
            "next_command": (
                f"TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES=1 "
                f"uv run python spikes/live_comments_read_e2e.py --alias {alias} --oauth"
            ),
        }

    account, tokens = loaded
    if account.status is not AccountStatus.OK:
        return {
            "status": "account_not_ok",
            "alias": alias,
            "account_status": account.status.value,
        }
    if tokens.access_token_expires_at <= datetime.now(UTC) + TOKEN_FRESHNESS_MARGIN:
        return {
            "status": "needs_oauth",
            "alias": alias,
            "reason": "access token is expired or too close to expiry for a no-refresh smoke run",
        }

    app_credentials = await load_app_credentials(backend, account.sandbox)
    original_refresh = BusinessAPIClient._refresh_tokens
    BusinessAPIClient._refresh_tokens = _refuse_token_refresh
    try:
        selected_video = await _select_video(
            account,
            app_credentials,
            tokens,
            backend,
            explicit_video_id=video_id,
        )
        if selected_video is None:
            return {
                "status": "needs_video_id",
                "alias": alias,
                "reason": "no owned videos were returned by the read-only video list endpoint",
            }

        comment_list_result = await comments_list(
            alias,
            video_id=selected_video["video_id"],
            max_count=30,
            status="ALL",
            sort_field="create_time",
            sort_order="desc",
        )
        comments = _comment_items(comment_list_result)
        top_level_comments = [
            comment
            for comment in comments
            if _optional_string(comment, "parent_comment_id") is None
        ]
        if not top_level_comments:
            return {
                "status": "needs_video_with_comments",
                "alias": alias,
                "video": selected_video,
                "comments_list": _summarize_comment_page(comment_list_result),
                "reason": (
                    "comments_list succeeded, but no top-level comment was available for replies"
                ),
            }

        reply_attempts: list[dict[str, object]] = []
        for comment in top_level_comments:
            comment_id = _required_string(comment, "comment_id")
            try:
                replies_result = await comments_list_replies(
                    alias,
                    video_id=selected_video["video_id"],
                    comment_id=comment_id,
                    max_count=1,
                    status="ALL",
                    sort_field="create_time",
                    sort_order="desc",
                )
            except Exception as exc:  # noqa: BLE001 - capture sanitized smoke diagnostics.
                reply_attempts.append(
                    {
                        "comment_id": _fingerprint(comment_id),
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                continue

            return {
                "status": "ok",
                "alias": alias,
                "read_only": True,
                "non_get_refresh_blocked": True,
                "video": selected_video,
                "comments_list": _summarize_comment_page(comment_list_result),
                "comments_list_replies": _summarize_comment_page(replies_result),
                "reply_comment_id": _fingerprint(comment_id),
                "endpoints": [
                    VIDEO_LIST_PATH if video_id is None else "explicit_video_id",
                    "/open_api/v1.3/business/comment/list/",
                    "/open_api/v1.3/business/comment/reply/list/",
                ],
            }

        return {
            "status": "comments_list_replies_failed",
            "alias": alias,
            "video": selected_video,
            "comments_list": _summarize_comment_page(comment_list_result),
            "reply_attempts": reply_attempts,
        }
    finally:
        BusinessAPIClient._refresh_tokens = original_refresh


async def _run_manual_oauth(alias: str) -> dict[str, object]:
    response = await add_account(ApiType.BUSINESS_ORGANIC, alias=alias, sandbox=False)
    if "error" in response:
        return response

    print("\nOpen this URL, approve the TikTok account-holder OAuth flow, then paste the full")
    print("redirect URL from the browser address bar back here. This script only uses")
    print("OAuth/token endpoints and read-only GET endpoints for the smoke test.\n")
    print(response["url"])
    print("\nRedirect URL: ", end="", flush=True)
    redirect_url = sys.stdin.readline().strip()
    if not redirect_url:
        return {"error": "missing_redirect_url"}
    return await complete_account_login(redirect_url, alias_override=alias)


async def _recover_manual_oauth(alias: str) -> dict[str, object]:
    print("Paste the full redirect URL. It will not be echoed back.")
    redirect_url = sys.stdin.readline().strip()
    if not redirect_url:
        return {"error": "missing_redirect_url"}

    state_token = _state_from_redirect(redirect_url)
    if state_token is None:
        return {"error": "missing_state"}

    oauth_state_store._STATES[state_token] = OAuthInProgress(
        state=state_token,
        api_type=ApiType.BUSINESS_ORGANIC,
        sandbox=False,
        pkce_verifier=None,
        suggested_alias=alias,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    return await complete_account_login(redirect_url, alias_override=alias)


def _state_from_redirect(redirect_url: str) -> str | None:
    state_values = parse_qs(urlparse(redirect_url).query).get("state")
    if not state_values:
        return None
    return state_values[0] or None


async def _load_account_with_tokens(
    backend: KeychainBackend,
    alias: str,
) -> tuple[Account, AccountTokens] | None:
    for sandbox in (False, True):
        raw_record = await backend.get(account_key(ApiType.BUSINESS_ORGANIC, sandbox, alias))
        if raw_record is None:
            continue
        return deserialize_account_record(raw_record)
    return None


async def _select_video(
    account: Account,
    app_credentials: AppCredentials,
    tokens: AccountTokens,
    backend: KeychainBackend,
    *,
    explicit_video_id: str | None,
) -> dict[str, object] | None:
    if explicit_video_id:
        return {"video_id": explicit_video_id, "source": "explicit"}

    async with BusinessAPIClient(
        account,
        app_credentials,
        tokens=tokens,
        backend=backend,
    ) as client:
        cursor: int | None = None
        seen_cursors: set[int] = set()
        videos: list[Mapping[str, object]] = []
        for _page in range(VIDEO_LIST_MAX_PAGES):
            params: dict[str, str | int] = {
                "business_id": account.tiktok_id,
                "fields": json.dumps(VIDEO_LIST_FIELDS, separators=(",", ":")),
                "max_count": 20,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = await client.get(VIDEO_LIST_PATH, params=params)
            videos.extend(
                video for video in _mapping_items(payload.get("videos")) if _video_id(video)
            )

            next_cursor = payload.get("cursor")
            if not payload.get("has_more") or not isinstance(next_cursor, int):
                break
            if next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    if not videos:
        return None

    videos.sort(
        key=lambda video: (
            _first_int(video.get("comments")),
            _first_int(video.get("create_time")),
        ),
        reverse=True,
    )
    selected = videos[0]
    return {
        "video_id": _video_id(selected),
        "source": "business_video_list",
        "reported_comment_count": _first_int(selected.get("comments")),
        "media_type": _optional_string(selected, "media_type"),
        "share_url_present": _optional_string(selected, "share_url") is not None,
    }


async def _refuse_token_refresh(
    self: BusinessAPIClient,
    tokens: AccountTokens,
) -> None:
    _ = self, tokens
    raise RuntimeError("read-only smoke refuses token refresh; re-run OAuth instead")


def _summarize_comment_page(page: Mapping[str, object]) -> dict[str, object]:
    comments = _comment_items(page)
    return {
        "video_id": _optional_string(page, "video_id"),
        "count": _first_int(page.get("count")),
        "cursor": _first_int(page.get("cursor")),
        "has_more": page.get("has_more"),
        "total": _first_int(page.get("total")),
        "comment_fingerprints": [
            {
                "comment_id": _fingerprint(_required_string(comment, "comment_id")),
                "parent_comment_id": _fingerprint(_optional_string(comment, "parent_comment_id")),
                "status": _optional_string(comment, "status"),
                "reply_count": _first_int(comment.get("reply_count")),
            }
            for comment in comments[:5]
        ],
    }


def _comment_items(page: Mapping[str, object]) -> list[Mapping[str, object]]:
    comments = page.get("comments")
    if not isinstance(comments, list):
        return []
    return [item for item in comments if isinstance(item, Mapping)]


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _video_id(video: Mapping[str, object]) -> str | None:
    return _optional_string(video, "item_id") or _optional_string(video, "video_id")


def _required_string(value: Mapping[str, object], key: str) -> str:
    candidate = _optional_string(value, key)
    if candidate is None:
        raise ValueError(f"missing string field {key}")
    return candidate


def _optional_string(value: Mapping[str, object] | None, key: str) -> str | None:
    if value is None:
        return None
    candidate = value.get(key)
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def _first_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def _fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    return f"{value[:4]}...len={len(value)}"


if __name__ == "__main__":
    raise SystemExit(main())
