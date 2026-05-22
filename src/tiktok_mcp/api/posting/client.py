"""Client for TikTok Content Posting read endpoints.

Content Posting uses Login Kit bearer-token auth like Display API, but it stays in
its own client for v0.1 to avoid premature cross-surface abstraction. Upload writes
default to TikTok drafts per the Decisions of Record; this read client only exposes
status polling and creator capability lookup needed before later write flows.
"""

from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import cast
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.auth.keychain import (
    KeychainBackend,
    app_creds_key,
    atomic_account_update,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.auth.redactor import register_token as add_runtime_token
from tiktok_mcp.envelopes import decode_display_response
from tiktok_mcp.observability.rate_limit_tracker import record_429, record_request
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountBrokenError,
    AccountNotFoundError,
    AppCredentialsNotSetError,
    RateLimitedError,
)

from .models import CreatorInfo, PostStatus

BASE_URL = "https://open.tiktokapis.com"
OAUTH_TOKEN_PATH = "/v2/oauth/token/"
POST_STATUS_PATH = "/v2/post/publish/status/fetch/"
CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
MAX_ATTEMPTS = 3
REFRESH_SKEW = timedelta(minutes=5)
REQUEST_TIMEOUT_SECONDS = 30.0

_REFRESH_LOCKS: dict[tuple[bool, str], asyncio.Lock] = {}
_REFRESH_LOCKS_GUARD = asyncio.Lock()
_FALLBACK_WAIT = wait_random_exponential(multiplier=0.5, max=8)


@dataclass(frozen=True)
class _RetryablePostingError(Exception):
    endpoint: str
    status_code: int | None = None
    request_id: str | None = None
    retry_after_seconds: float | None = None


class PostingAPIClient:
    def __init__(
        self,
        *,
        backend: KeychainBackend | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._backend: KeychainBackend | None = backend
        self._http_client: httpx.AsyncClient | None = http_client
        self._owns_http_client: bool = http_client is None
        self._base_url: str = base_url.rstrip("/")

    async def __aenter__(self) -> PostingAPIClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        _ = exc_type, exc, traceback
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def get_post_status(self, alias: str, publish_id: str) -> PostStatus:
        data = await self.request(
            alias,
            "POST",
            POST_STATUS_PATH,
            json_body={"publish_id": publish_id},
        )
        return PostStatus.model_validate(data)

    async def get_creator_info(self, alias: str) -> CreatorInfo:
        """Fetch creator posting capability info without caching.

        TikTok privacy options can change in the app, so callers must fetch this
        live before initiating an upload rather than relying on cached values.
        """
        data = await self.request(alias, "POST", CREATOR_INFO_PATH, json_body={})
        return CreatorInfo.model_validate(data)

    async def list_drafts(
        self,
        alias: str,
        *,
        max_count: int = 20,
        cursor: int | None = None,
    ) -> dict[str, object]:
        _ = alias, max_count, cursor
        return drafts_endpoint_not_available()

    async def request(
        self,
        alias: str,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
    ) -> dict[str, object]:
        if method.upper() != "POST":
            raise ValueError("Content Posting client currently supports POST JSON requests only")
        response = await self._post_authenticated(alias, path, json_body=json_body)
        return cast(dict[str, object], decode_display_response(response))

    async def put_chunk_to_url(
        self,
        alias: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
    ) -> httpx.Response:
        _account, tokens = await self._load_fresh_tokens(alias)
        access_token = tokens.access_token.get_secret_value()
        add_runtime_token(access_token, "access_token")
        response = await self._put_chunk_once(
            alias,
            url,
            headers=_chunk_headers(headers, access_token),
            content=content,
        )
        if response.status_code != 401:
            await self._raise_for_chunk_upload(alias, response)
            return response

        refreshed_access_token = await self._refresh_after_unauthorized(alias, access_token)
        response = await self._put_chunk_once(
            alias,
            url,
            headers=_chunk_headers(headers, refreshed_access_token),
            content=content,
        )
        await self._raise_for_chunk_upload(alias, response)
        return response

    async def _post_authenticated(
        self,
        alias: str,
        path: str,
        *,
        json_body: Mapping[str, object],
    ) -> httpx.Response:
        _account, tokens = await self._load_fresh_tokens(alias)
        access_token = tokens.access_token.get_secret_value()
        add_runtime_token(access_token, "access_token")
        return await self._post_with_retry(
            alias,
            path,
            headers={"Authorization": f"Bearer {access_token}"},
            json_body=json_body,
        )

    async def _load_fresh_tokens(self, alias: str) -> tuple[Account, AccountTokens]:
        account, tokens = await self._load_account(alias)
        if not _needs_refresh(tokens):
            return account, tokens

        lock = await _refresh_lock(account)
        async with lock:
            account, tokens = await self._load_account(alias)
            if not _needs_refresh(tokens):
                return account, tokens
            refreshed_tokens = await self._refresh_tokens(account, tokens)
            backend = await self._get_backend()
            await atomic_account_update(
                backend,
                account.api_type,
                account.sandbox,
                account.alias,
                account,
                refreshed_tokens,
            )
            return account, refreshed_tokens

    async def _load_account(self, alias: str) -> tuple[Account, AccountTokens]:
        backend = await self._get_backend()
        for key in await backend.list_keys("tiktok-mcp::content_posting::"):
            if not key.endswith(f"::account::{alias}"):
                continue
            raw_record = await backend.get(key)
            if raw_record is None:
                continue
            account, tokens = deserialize_account_record(raw_record)
            if account.api_type is not ApiType.CONTENT_POSTING:
                continue
            if account.status is not AccountStatus.OK:
                raise AccountBrokenError(alias, status=account.status.value)
            _register_tokens(tokens)
            return account, tokens
        raise AccountNotFoundError(alias, api_type=ApiType.CONTENT_POSTING.value)

    async def _refresh_after_unauthorized(
        self,
        alias: str,
        invalid_access_token: str,
    ) -> str:
        account, _tokens = await self._load_account(alias)
        lock = await _refresh_lock(account)
        async with lock:
            account, tokens = await self._load_account(alias)
            current_access_token = tokens.access_token.get_secret_value()
            if current_access_token != invalid_access_token:
                return current_access_token
            refreshed_tokens = await self._refresh_tokens(account, tokens)
            backend = await self._get_backend()
            await atomic_account_update(
                backend,
                account.api_type,
                account.sandbox,
                account.alias,
                account,
                refreshed_tokens,
            )
            return refreshed_tokens.access_token.get_secret_value()

    async def _refresh_tokens(self, account: Account, tokens: AccountTokens) -> AccountTokens:
        credentials = await self._load_app_credentials(account.sandbox)
        refresh_token = tokens.refresh_token.get_secret_value()
        add_runtime_token(refresh_token, "refresh_token")
        response = await self._post_with_retry(
            account.alias,
            OAUTH_TOKEN_PATH,
            data={
                "client_key": credentials.client_id.get_secret_value(),
                "client_secret": credentials.client_secret.get_secret_value(),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            raise SanitizedHttpxError(
                status=response.status_code,
                url_path=response.request.url.path,
                request_id=_request_id(response),
            )
        payload = _json_object(response)
        refreshed_tokens = _tokens_from_refresh_payload(tokens, payload)
        _register_tokens(refreshed_tokens)
        return refreshed_tokens

    async def _load_app_credentials(self, sandbox: bool) -> AppCredentials:
        backend = await self._get_backend()
        raw_credentials = await backend.get(app_creds_key(ApiType.CONTENT_POSTING, sandbox))
        if raw_credentials is None:
            raise AppCredentialsNotSetError(ApiType.CONTENT_POSTING.value, sandbox)
        try:
            payload = cast(object, json.loads(raw_credentials))
        except json.JSONDecodeError as exc:
            raise AppCredentialsNotSetError(ApiType.CONTENT_POSTING.value, sandbox) from exc
        if not isinstance(payload, dict):
            raise AppCredentialsNotSetError(ApiType.CONTENT_POSTING.value, sandbox)
        payload_mapping = cast(dict[object, object], payload)
        credentials_payload = _credentials_payload(
            {str(key): value for key, value in payload_mapping.items()}
        )
        try:
            credentials = AppCredentials.model_validate(credentials_payload)
        except ValidationError as exc:
            raise AppCredentialsNotSetError(ApiType.CONTENT_POSTING.value, sandbox) from exc
        add_runtime_token(credentials.client_id.get_secret_value(), "client_id")
        add_runtime_token(credentials.client_secret.get_secret_value(), "client_secret")
        return credentials

    async def _post_with_retry(
        self,
        alias: str,
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_RetryablePostingError),
                stop=stop_after_attempt(MAX_ATTEMPTS),
                wait=_retry_wait,
                reraise=True,
            ):
                with attempt:
                    return await self._post_once(
                        alias,
                        path,
                        headers=headers,
                        json_body=json_body,
                        data=data,
                    )
        except _RetryablePostingError as exc:
            _raise_terminal_retry_error(exc)
        raise RuntimeError("unreachable retry state")

    async def _post_once(
        self,
        alias: str,
        path: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        data: Mapping[str, str] | None,
    ) -> httpx.Response:
        await record_request(ApiType.CONTENT_POSTING, alias)
        try:
            response = await self._client().post(
                f"{self._base_url}{path}",
                headers=dict(headers),
                json=dict(json_body) if json_body is not None else None,
                data=dict(data) if data is not None else None,
            )
        except httpx.HTTPError as exc:
            raise _RetryablePostingError(endpoint=path) from exc

        if response.status_code == 429:
            retry_after_seconds = _retry_after_seconds(_header(response, "Retry-After"))
            await record_429(ApiType.CONTENT_POSTING, alias, retry_after_seconds)
            raise _RetryablePostingError(
                endpoint=path,
                status_code=response.status_code,
                request_id=_request_id(response),
                retry_after_seconds=retry_after_seconds,
            )
        if 500 <= response.status_code < 600:
            raise _RetryablePostingError(
                endpoint=path,
                status_code=response.status_code,
                request_id=_request_id(response),
            )
        return response

    async def _put_chunk_once(
        self,
        alias: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
    ) -> httpx.Response:
        await record_request(ApiType.CONTENT_POSTING, alias)
        try:
            return await self._client().put(url, headers=dict(headers), content=content)
        except httpx.HTTPError as exc:
            raise SanitizedHttpxError(status=0, url_path=_url_path(url)) from exc

    async def _raise_for_chunk_upload(self, alias: str, response: httpx.Response) -> None:
        if response.status_code == 429:
            retry_after_seconds = _retry_after_seconds(_header(response, "Retry-After"))
            await record_429(ApiType.CONTENT_POSTING, alias, retry_after_seconds)
            raise RateLimitedError(
                retry_after=retry_after_seconds,
                attempts=1,
                context={"endpoint": response.request.url.path},
            )
        if response.status_code >= 400:
            raise SanitizedHttpxError(
                status=response.status_code,
                url_path=response.request.url.path,
                request_id=_request_id(response),
            )

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        return self._http_client

    async def _get_backend(self) -> KeychainBackend:
        if self._backend is not None:
            return self._backend
        return await get_backend()


def drafts_endpoint_not_available() -> dict[str, object]:
    return {
        "endpoint_not_available": True,
        "reason": "TikTok has not exposed a drafts-list endpoint in v2 as of 2026-05-22",
    }


async def _refresh_lock(account: Account) -> asyncio.Lock:
    key = (account.sandbox, account.alias)
    async with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _REFRESH_LOCKS[key] = lock
        return lock


def _needs_refresh(tokens: AccountTokens) -> bool:
    return tokens.access_token_expires_at <= datetime.now(UTC) + REFRESH_SKEW


def _tokens_from_refresh_payload(
    previous_tokens: AccountTokens,
    payload: Mapping[str, object],
) -> AccountTokens:
    now = datetime.now(UTC)
    access_token = _string_value(payload, "access_token")
    refresh_token = (
        _optional_string_value(payload, "refresh_token")
        or previous_tokens.refresh_token.get_secret_value()
    )
    expires_in = _int_value(payload, "expires_in")
    refresh_expires_in = _optional_int_value(payload, "refresh_expires_in")
    refresh_expires_at = (
        now + timedelta(seconds=refresh_expires_in)
        if refresh_expires_in is not None
        else previous_tokens.refresh_token_expires_at
    )
    return AccountTokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token),
        access_token_expires_at=now + timedelta(seconds=expires_in),
        refresh_token_expires_at=refresh_expires_at,
        last_rotated_at=now,
    )


def _credentials_payload(payload: Mapping[str, object]) -> dict[str, object]:
    nested_credentials = payload.get("credentials")
    if isinstance(nested_credentials, dict):
        nested_mapping = cast(dict[object, object], nested_credentials)
        source = {str(key): value for key, value in nested_mapping.items()}
    else:
        source = dict(payload)
    return {
        key: source[key]
        for key in {"api_type", "sandbox", "client_id", "client_secret", "created_at"}
        if key in source
    }


def _register_tokens(tokens: AccountTokens) -> None:
    add_runtime_token(tokens.access_token.get_secret_value(), "access_token")
    add_runtime_token(tokens.refresh_token.get_secret_value(), "refresh_token")


def _chunk_headers(headers: Mapping[str, str], access_token: str) -> dict[str, str]:
    prepared_headers = dict(headers)
    prepared_headers["Authorization"] = f"Bearer {access_token}"
    return prepared_headers


def _retry_wait(retry_state: RetryCallState) -> float:
    outcome = retry_state.outcome
    if outcome is not None and outcome.failed:
        exc = outcome.exception()
        if isinstance(exc, _RetryablePostingError) and exc.retry_after_seconds is not None:
            return exc.retry_after_seconds
    return float(_FALLBACK_WAIT(retry_state))


def _raise_terminal_retry_error(exc: _RetryablePostingError) -> None:
    if exc.status_code == 429:
        raise RateLimitedError(
            retry_after=exc.retry_after_seconds,
            attempts=MAX_ATTEMPTS,
            context={"endpoint": exc.endpoint},
        ) from exc
    raise SanitizedHttpxError(
        status=exc.status_code or 0,
        url_path=exc.endpoint,
        request_id=exc.request_id,
    ) from exc


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        retry_after = (retry_at - datetime.now(UTC)).total_seconds()
    return max(retry_after, 0.0)


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = cast(object, response.json())
    except ValueError as exc:
        raise SanitizedHttpxError(
            status=response.status_code,
            url_path=response.request.url.path,
            request_id=_request_id(response),
        ) from exc
    if not isinstance(payload, dict):
        raise SanitizedHttpxError(
            status=response.status_code,
            url_path=response.request.url.path,
            request_id=_request_id(response),
        )
    payload_mapping = cast(dict[object, object], payload)
    return {str(key): value for key, value in payload_mapping.items()}


def _request_id(response: httpx.Response) -> str | None:
    return _header(response, "x-tt-logid")


def _header(response: httpx.Response, name: str) -> str | None:
    return cast(str | None, response.headers.get(name))


def _url_path(url: str) -> str:
    path = urlparse(url).path
    return path or url


def _int_value(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SanitizedHttpxError(status=200, url_path=OAUTH_TOKEN_PATH)
    return value


def _optional_int_value(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SanitizedHttpxError(status=200, url_path=OAUTH_TOKEN_PATH)
    return value


def _string_value(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    raise SanitizedHttpxError(status=200, url_path=OAUTH_TOKEN_PATH)


def _optional_string_value(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "BASE_URL",
    "CREATOR_INFO_PATH",
    "POST_STATUS_PATH",
    "PostingAPIClient",
    "drafts_endpoint_not_available",
]
