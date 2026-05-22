from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, NoReturn

import httpx
from pydantic import SecretStr
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential_jitter

from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError, install_httpx_sanitization
from tiktok_mcp.auth.keychain import (
    account_key,
    atomic_account_update,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.auth.redactor import register_token
from tiktok_mcp.envelopes import decode_display_response
from tiktok_mcp.observability.rate_limit_tracker import record_429, record_request
from tiktok_mcp.types import (
    Account,
    AccountBrokenError,
    AccountNotFoundError,
    AccountStatus,
    ApiType,
    DisplayApiError,
    KeychainUnavailableError,
    RateLimitedError,
    TikTokMCPError,
)
from tiktok_mcp.types.accounts import AccountTokens
from tiktok_mcp.types.app_credentials import AppCredentials

DISPLAY_BASE_URL = "https://open.tiktokapis.com"
DISPLAY_TOKEN_PATH = "/v2/oauth/token/"
_REFRESH_MARGIN = timedelta(minutes=5)
_MAX_RETRY_ATTEMPTS = 3
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_UNAUTHORIZED = 401
_HTTP_INTERNAL_SERVER_ERROR = 500
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_REFRESH_LOCKS: dict[tuple[ApiType, bool, str], asyncio.Lock] = {}
_JITTER_WAIT = wait_exponential_jitter(initial=1, max=30)
QueryParams = Mapping[str, str | int | float | bool | None]

logger = logging.getLogger(__name__)


class _RetryableDisplayHTTPStatusError(Exception):
    def __init__(
        self,
        status_code: int,
        url_path: str,
        retry_after_seconds: float | None,
        request_id: str | None,
    ) -> None:
        self.status_code: int = status_code
        self.url_path: str = url_path
        self.retry_after_seconds: float | None = retry_after_seconds
        self.request_id: str | None = request_id
        super().__init__(f"retryable Display API HTTP status {status_code}")


class DisplayAPIClient:
    def __init__(self, account: Account, app_credentials: AppCredentials) -> None:
        self.account: Account = account
        self.app_credentials: AppCredentials = app_credentials
        self._client: httpx.AsyncClient | None = None
        self._tokens: AccountTokens | None = None
        self._refresh_lock: asyncio.Lock = _REFRESH_LOCKS.setdefault(
            (account.api_type, account.sandbox, account.alias), asyncio.Lock()
        )
        self._retry_after_by_response: dict[tuple[int, str, str | None], float | None] = {}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_fresh_token(self) -> str:
        tokens = self._tokens
        if tokens is None:
            _account, tokens = await self._load_account_record()
            self._tokens = tokens

        if self.account.status is AccountStatus.BROKEN:
            raise AccountBrokenError(self.account.alias, status=self.account.status.value)

        if not _expires_within_refresh_margin(tokens):
            return tokens.access_token.get_secret_value()

        async with self._refresh_lock:
            stored_account, stored_tokens = await self._load_account_record()
            self.account = stored_account
            self._tokens = stored_tokens
            if stored_account.status is AccountStatus.BROKEN:
                raise AccountBrokenError(stored_account.alias, status=stored_account.status.value)
            if not _expires_within_refresh_margin(stored_tokens):
                return stored_tokens.access_token.get_secret_value()
            refreshed_tokens = await self._refresh_tokens(stored_account, stored_tokens)
            self._tokens = refreshed_tokens
            return refreshed_tokens.access_token.get_secret_value()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json: Any | None = None,
    ) -> Any:
        access_token = await self._ensure_fresh_token()
        try:
            return await self._request_with_retries(
                method,
                path,
                access_token,
                params=params,
                json=json,
            )
        except DisplayApiError as exc:
            if not _is_access_token_invalid(exc):
                raise

        refreshed_access_token = await self._refresh_after_invalid_token(access_token)
        try:
            return await self._request_with_retries(
                method,
                path,
                refreshed_access_token,
                params=params,
                json=json,
            )
        except DisplayApiError as exc:
            if _is_access_token_invalid(exc):
                await self._mark_account_broken()
                raise AccountBrokenError(
                    self.account.alias,
                    status=AccountStatus.BROKEN.value,
                    context={"endpoint": path},
                ) from exc
            raise

    async def _request_with_retries(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        params: QueryParams | None,
        json: Any | None,
    ) -> Any:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(_MAX_RETRY_ATTEMPTS),
                wait=_wait_retry_after_or_jitter,
                retry=retry_if_exception_type(_RetryableDisplayHTTPStatusError),
                reraise=True,
            ):
                with attempt:
                    return await self._request_once(
                        method,
                        path,
                        access_token,
                        params=params,
                        json=json,
                    )
        except _RetryableDisplayHTTPStatusError as exc:
            if exc.status_code == _HTTP_TOO_MANY_REQUESTS:
                raise RateLimitedError(
                    exc.retry_after_seconds,
                    _MAX_RETRY_ATTEMPTS,
                    context={"api_type": ApiType.DISPLAY.value, "alias": self.account.alias},
                ) from exc
            raise DisplayApiError(
                http_status=exc.status_code,
                message="Display API server error.",
                error_code="server_error",
                context={"endpoint": exc.url_path, "request_id": exc.request_id},
            ) from exc

        msg = "Display API retry loop ended without a response."
        raise RuntimeError(msg)

    async def _request_once(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        params: QueryParams | None,
        json: Any | None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {access_token}"}
        normalized_method = method.upper()
        if normalized_method == "POST":
            headers["Content-Type"] = "application/json"

        try:
            response = await self._http_client.request(
                normalized_method,
                path,
                params=params,
                json=json,
                headers=headers,
            )
        except SanitizedHttpxError as exc:
            await self._handle_sanitized_http_error(exc, normalized_method)
        except httpx.HTTPError as exc:
            raise DisplayApiError(
                http_status=0,
                message="Display API request failed.",
                error_code=type(exc).__name__,
                context={"endpoint": path},
            ) from exc

        decoded = decode_display_response(response)
        await record_request(api_type=ApiType.DISPLAY, alias=self.account.alias)
        return decoded

    async def _handle_sanitized_http_error(
        self,
        exc: SanitizedHttpxError,
        method: str,
    ) -> NoReturn:
        retry_after_seconds = self._pop_retry_after(exc)
        if exc.status == _HTTP_TOO_MANY_REQUESTS:
            await record_429(
                api_type=ApiType.DISPLAY,
                alias=self.account.alias,
                retry_after_seconds=retry_after_seconds,
            )
            if method in _IDEMPOTENT_METHODS:
                raise _RetryableDisplayHTTPStatusError(
                    exc.status,
                    exc.url_path,
                    retry_after_seconds,
                    exc.request_id,
                ) from exc
            raise RateLimitedError(
                retry_after_seconds,
                1,
                context={"api_type": ApiType.DISPLAY.value, "alias": self.account.alias},
            ) from exc

        if exc.status >= _HTTP_INTERNAL_SERVER_ERROR and method in _IDEMPOTENT_METHODS:
            raise _RetryableDisplayHTTPStatusError(
                exc.status,
                exc.url_path,
                retry_after_seconds,
                exc.request_id,
            ) from exc

        if exc.status == _HTTP_UNAUTHORIZED:
            raise DisplayApiError(
                http_status=exc.status,
                message="Access token invalid.",
                error_code="access_token_invalid",
                context={"endpoint": exc.url_path, "request_id": exc.request_id},
            ) from exc

        raise DisplayApiError(
            http_status=exc.status,
            message=str(exc),
            error_code="http_error",
            context={"endpoint": exc.url_path, "request_id": exc.request_id},
        ) from exc

    async def _capture_retry_after(self, response: httpx.Response) -> None:
        if response.status_code == _HTTP_TOO_MANY_REQUESTS:
            self._retry_after_by_response[
                (
                    response.status_code,
                    response.request.url.path,
                    response.headers.get("x-tt-logid"),
                )
            ] = _parse_retry_after(response.headers.get("Retry-After"))

    def _pop_retry_after(self, exc: SanitizedHttpxError) -> float | None:
        return self._retry_after_by_response.pop(
            (exc.status, exc.url_path, exc.request_id),
            None,
        )

    @property
    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            client = self._build_http_client()
            client.event_hooks.setdefault("response", []).insert(0, self._capture_retry_after)
            install_httpx_sanitization(client)
            self._client = client
        return self._client

    def _build_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30.0, base_url=DISPLAY_BASE_URL)

    async def _load_account_record(self) -> tuple[Account, AccountTokens]:
        backend = await get_backend()
        key = account_key(self.account.api_type, self.account.sandbox, self.account.alias)
        raw_record = await backend.get(key)
        if raw_record is None:
            raise AccountNotFoundError(
                self.account.alias,
                api_type=self.account.api_type.value,
                sandbox=self.account.sandbox,
            )
        account, tokens = deserialize_account_record(raw_record)
        _register_account_tokens(tokens)
        return account, tokens

    async def _refresh_after_invalid_token(self, invalid_access_token: str) -> str:
        async with self._refresh_lock:
            stored_account, stored_tokens = await self._load_account_record()
            current_access_token = stored_tokens.access_token.get_secret_value()
            self.account = stored_account
            self._tokens = stored_tokens
            if current_access_token != invalid_access_token:
                return current_access_token
            refreshed_tokens = await self._refresh_tokens(stored_account, stored_tokens)
            self._tokens = refreshed_tokens
            return refreshed_tokens.access_token.get_secret_value()

    async def _refresh_tokens(self, account: Account, tokens: AccountTokens) -> AccountTokens:
        body = {
            "client_key": self.app_credentials.client_id.get_secret_value(),
            "client_secret": self.app_credentials.client_secret.get_secret_value(),
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token.get_secret_value(),
        }
        try:
            response = await self._http_client.post(
                DISPLAY_TOKEN_PATH,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except SanitizedHttpxError as exc:
            raise DisplayApiError(
                http_status=exc.status,
                message="Display OAuth token refresh failed.",
                error_code="token_refresh_failed",
                context={"endpoint": exc.url_path, "request_id": exc.request_id},
            ) from exc
        except httpx.HTTPError as exc:
            raise DisplayApiError(
                http_status=0,
                message="Display OAuth token refresh failed.",
                error_code=type(exc).__name__,
                context={"endpoint": DISPLAY_TOKEN_PATH},
            ) from exc

        payload = _json_object(response)
        refreshed_tokens = _tokens_from_refresh_payload(payload, tokens)
        _register_account_tokens(refreshed_tokens)
        await self._persist_refreshed_tokens(account, refreshed_tokens)
        return refreshed_tokens

    async def _persist_refreshed_tokens(self, account: Account, tokens: AccountTokens) -> None:
        backend = await get_backend()
        try:
            await atomic_account_update(
                backend,
                account.api_type,
                account.sandbox,
                account.alias,
                account,
                tokens,
            )
        except TikTokMCPError:
            logger.exception(
                "Display token refresh keychain write failed for alias=%s",
                account.alias,
            )
            raise
        except Exception as exc:
            logger.exception(
                "Display token refresh keychain write failed for alias=%s",
                account.alias,
            )
            raise KeychainUnavailableError(
                "Failed to store refreshed Display account tokens.",
                context={"alias": account.alias},
            ) from exc

    async def _mark_account_broken(self) -> None:
        async with self._refresh_lock:
            account, tokens = await self._load_account_record()
            broken_account = account.model_copy(update={"status": AccountStatus.BROKEN})
            backend = await get_backend()
            await atomic_account_update(
                backend,
                broken_account.api_type,
                broken_account.sandbox,
                broken_account.alias,
                broken_account,
                tokens,
            )
            self.account = broken_account
            self._tokens = tokens


def _expires_within_refresh_margin(tokens: AccountTokens) -> bool:
    return tokens.access_token_expires_at < datetime.now(UTC) + _REFRESH_MARGIN


def _is_access_token_invalid(exc: DisplayApiError) -> bool:
    return exc.error_code == "access_token_invalid"


def _register_account_tokens(tokens: AccountTokens) -> None:
    register_token(tokens.access_token.get_secret_value(), "access_token")
    register_token(tokens.refresh_token.get_secret_value(), "refresh_token")


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DisplayApiError(
            http_status=response.status_code,
            message="Malformed Display OAuth token refresh response.",
            error_code="malformed_token_refresh_response",
            context={"endpoint": response.request.url.path},
        ) from exc
    if not isinstance(payload, dict):
        raise DisplayApiError(
            http_status=response.status_code,
            message="Display OAuth token refresh response must be a JSON object.",
            error_code="malformed_token_refresh_response",
            context={"endpoint": response.request.url.path},
        )
    return {str(key): value for key, value in payload.items()}


def _tokens_from_refresh_payload(
    payload: Mapping[str, object],
    previous_tokens: AccountTokens,
) -> AccountTokens:
    now = datetime.now(UTC)
    access_token = _required_string(payload, "access_token")
    refresh_token = _optional_string(payload, "refresh_token")
    expires_in = _required_int(payload, "expires_in")
    refresh_expires_in = _optional_int(payload, "refresh_expires_in")
    return AccountTokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token or previous_tokens.refresh_token.get_secret_value()),
        access_token_expires_at=now + timedelta(seconds=expires_in),
        refresh_token_expires_at=(
            now + timedelta(seconds=refresh_expires_in)
            if refresh_expires_in is not None
            else previous_tokens.refresh_token_expires_at
        ),
        last_rotated_at=now,
    )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    raise DisplayApiError(
        http_status=200,
        message=f"Display OAuth token refresh response is missing string field {key}.",
        error_code="malformed_token_refresh_response",
    )


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DisplayApiError(
            http_status=200,
            message=f"Display OAuth token refresh response is missing integer field {key}.",
            error_code="malformed_token_refresh_response",
        )
    return value


def _optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DisplayApiError(
            http_status=200,
            message=f"Display OAuth token refresh response field {key} must be an integer.",
            error_code="malformed_token_refresh_response",
        )
    return value


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _wait_retry_after_or_jitter(retry_state: RetryCallState) -> float:
    exception = retry_state.outcome.exception() if retry_state.outcome is not None else None
    if (
        isinstance(exception, _RetryableDisplayHTTPStatusError)
        and exception.retry_after_seconds is not None
    ):
        return exception.retry_after_seconds
    return float(_JITTER_WAIT(retry_state))


__all__ = ["AccountBrokenError", "DisplayAPIClient"]
