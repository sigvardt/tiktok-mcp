from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, ClassVar, Self, TypeVar, cast

import httpx
from httpx._types import RequestData, RequestFiles
from pydantic import BaseModel, SecretStr
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError, install_httpx_sanitization
from tiktok_mcp.auth.keychain import (
    KeychainBackend,
    account_key,
    atomic_account_update,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.auth.redactor import register_token as add_runtime_token
from tiktok_mcp.envelopes import decode_business_response
from tiktok_mcp.observability.rate_limit_tracker import record_429, record_request
from tiktok_mcp.types.accounts import (
    Account,
    AccountStatus,
    AccountTokens,
    AccountWithTokens,
    ApiType,
)
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountBrokenError,
    AccountNotFoundError,
    BusinessApiError,
)

DataModelT = TypeVar("DataModelT", bound=BaseModel)
QueryParams = Mapping[str, str | int | float | bool | None]

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_AUTH_ERROR_CODES = frozenset({40100, 40105})
_HTTP_TOO_MANY_REQUESTS = 429
_RETRY_AFTER_SECONDS: ContextVar[float | None] = ContextVar(
    "business_api_retry_after_seconds",
    default=None,
)
_REFRESH_LOCKS: dict[tuple[ApiType, bool, str], asyncio.Lock] = {}
_REFRESH_LOCKS_GUARD = asyncio.Lock()


class BusinessAPIClient:
    BASE_URL: ClassVar[str] = "https://business-api.tiktok.com"
    REFRESH_PATH: ClassVar[str] = "/open_api/v1.3/oauth2/refresh_token/"
    MAX_ATTEMPTS: ClassVar[int] = 3

    def __init__(
        self,
        account: Account | AccountWithTokens,
        app_credentials: AppCredentials,
        tokens: AccountTokens | None = None,
        *,
        backend: KeychainBackend | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.account: Account = _account_without_token_fields(account)
        self.app_credentials: AppCredentials = app_credentials
        self.tokens: AccountTokens | None = (
            tokens if tokens is not None else _tokens_from_account(account)
        )
        self._backend: KeychainBackend | None = backend
        self._transport: httpx.AsyncBaseTransport | None = transport
        self._timeout: float = timeout
        self._client: httpx.AsyncClient | None = None

        if self.tokens is not None:
            _register_tokens(self.tokens)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, traceback)
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(
        self,
        path: str,
        *,
        params: QueryParams | None = None,
        data_model: type[DataModelT] | None = None,
    ) -> DataModelT | dict[str, Any]:
        return await self.request("GET", path, params=params, data_model=data_model)

    async def post(
        self,
        path: str,
        *,
        params: QueryParams | None = None,
        json: object | None = None,
        data: RequestData | None = None,
        files: RequestFiles | None = None,
        data_model: type[DataModelT] | None = None,
        idempotent: bool = False,
    ) -> DataModelT | dict[str, Any]:
        return await self.request(
            "POST",
            path,
            params=params,
            json=json,
            data=data,
            files=files,
            data_model=data_model,
            idempotent=idempotent,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json: object | None = None,
        data: RequestData | None = None,
        files: RequestFiles | None = None,
        data_model: type[DataModelT] | None = None,
        idempotent: bool | None = None,
    ) -> DataModelT | dict[str, Any]:
        try:
            return await self._request_with_retries(
                method,
                path,
                params=params,
                json=json,
                data=data,
                files=files,
                data_model=data_model,
                idempotent=idempotent,
            )
        except BusinessApiError as exc:
            if exc.tiktok_code not in _AUTH_ERROR_CODES:
                raise
            return await self._refresh_then_retry_once(
                exc,
                method,
                path,
                params=params,
                json=json,
                data=data,
                files=files,
                data_model=data_model,
                idempotent=idempotent,
            )

    async def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None,
        json: object | None,
        data: RequestData | None,
        files: RequestFiles | None,
        data_model: type[DataModelT] | None,
        idempotent: bool | None,
    ) -> DataModelT | dict[str, Any]:
        method_upper = method.upper()
        should_retry = idempotent if idempotent is not None else method_upper in _IDEMPOTENT_METHODS
        if not should_retry:
            return await self._send_once(
                method_upper,
                path,
                params=params,
                json=json,
                data=data,
                files=files,
                data_model=data_model,
            )

        retryer = AsyncRetrying(
            retry=retry_if_exception(_is_retryable_http_status),
            wait=_retry_wait_seconds,
            stop=stop_after_attempt(self.MAX_ATTEMPTS),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                return await self._send_once(
                    method_upper,
                    path,
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                    data_model=data_model,
                )
        raise AssertionError("unreachable retry state")

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None,
        json: object | None,
        data: RequestData | None,
        files: RequestFiles | None,
        data_model: type[DataModelT] | None,
    ) -> DataModelT | dict[str, Any]:
        _ = _RETRY_AFTER_SECONDS.set(None)
        tokens = await self._ensure_tokens()
        client = await self._http_client()
        response = await client.request(
            method,
            path,
            params=params,
            json=json,
            data=data,
            files=files,
            headers=self._auth_headers(tokens),
        )
        decoded = decode_business_response(response, data_model=data_model)
        await record_request(self.account.api_type, self.account.alias)
        return decoded

    async def _refresh_then_retry_once(
        self,
        auth_error: BusinessApiError,
        method: str,
        path: str,
        *,
        params: QueryParams | None,
        json: object | None,
        data: RequestData | None,
        files: RequestFiles | None,
        data_model: type[DataModelT] | None,
        idempotent: bool | None,
    ) -> DataModelT | dict[str, Any]:
        tokens = await self._ensure_tokens()
        failed_access_token = tokens.access_token.get_secret_value()
        if not self._has_refresh_token(tokens):
            raise self._account_broken_error(auth_error) from auth_error

        async with await _refresh_lock_for(self.account):
            current_tokens = await self._refresh_path_tokens()
            if current_tokens.access_token.get_secret_value() == failed_access_token:
                try:
                    await self._refresh_tokens(current_tokens)
                except BusinessApiError as exc:
                    await self._mark_account_broken()
                    raise self._account_broken_error(exc) from exc

        try:
            return await self._request_with_retries(
                method,
                path,
                params=params,
                json=json,
                data=data,
                files=files,
                data_model=data_model,
                idempotent=idempotent,
            )
        except BusinessApiError as exc:
            if exc.tiktok_code in _AUTH_ERROR_CODES:
                await self._mark_account_broken()
                raise self._account_broken_error(exc) from exc
            raise

    async def _refresh_tokens(self, tokens: AccountTokens) -> None:
        refresh_token = tokens.refresh_token.get_secret_value()
        body = {
            "app_id": self.app_credentials.client_id.get_secret_value(),
            "secret": self.app_credentials.client_secret.get_secret_value(),
            "refresh_token": refresh_token,
        }
        client = await self._http_client()
        response = await client.post(self.REFRESH_PATH, json=body)
        payload = decode_business_response(response)
        await record_request(self.account.api_type, self.account.alias)

        new_access_token = _required_string(payload, "access_token")
        new_refresh_token = _optional_string(payload, "refresh_token") or refresh_token

        now = datetime.now(UTC)
        access_expires_at = now + timedelta(seconds=_required_int(payload, "expires_in"))
        refresh_expires_in = _optional_int(payload, "refresh_expires_in")
        refresh_expires_at = (
            now + timedelta(seconds=refresh_expires_in) if refresh_expires_in is not None else None
        )
        new_tokens = AccountTokens(
            access_token=SecretStr(new_access_token),
            refresh_token=SecretStr(new_refresh_token),
            access_token_expires_at=access_expires_at,
            refresh_token_expires_at=refresh_expires_at,
            last_rotated_at=now,
        )
        backend = await self._keychain_backend()
        await atomic_account_update(
            backend,
            self.account.api_type,
            self.account.sandbox,
            self.account.alias,
            self.account,
            new_tokens,
        )
        self.tokens = new_tokens
        _register_tokens(new_tokens)

    async def _mark_account_broken(self) -> None:
        tokens = await self._ensure_tokens()
        self.account.status = AccountStatus.BROKEN
        backend = await self._keychain_backend()
        await atomic_account_update(
            backend,
            self.account.api_type,
            self.account.sandbox,
            self.account.alias,
            self.account,
            tokens,
        )

    async def _ensure_tokens(self) -> AccountTokens:
        if self.tokens is None:
            account, tokens = await self._load_account_record()
            self.account = account
            self.tokens = tokens

        if self.account.status is AccountStatus.BROKEN:
            raise AccountBrokenError(self.account.alias, status=self.account.status.value)
        return self.tokens

    async def _refresh_path_tokens(self) -> AccountTokens:
        try:
            account, tokens = await self._load_account_record()
        except AccountNotFoundError:
            return await self._ensure_tokens()
        self.account = account
        self.tokens = tokens
        return tokens

    async def _load_account_record(self) -> tuple[Account, AccountTokens]:
        backend = await self._keychain_backend()
        key = account_key(self.account.api_type, self.account.sandbox, self.account.alias)
        raw_record = await backend.get(key)
        if raw_record is None:
            raise AccountNotFoundError(
                self.account.alias,
                api_type=self.account.api_type.value,
                sandbox=self.account.sandbox,
            )
        account, tokens = deserialize_account_record(raw_record)
        _register_tokens(tokens)
        return account, tokens

    async def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                timeout=self._timeout,
                transport=self._transport,
                event_hooks={"response": [self._capture_rate_limit]},
            )
            install_httpx_sanitization(client)
            self._client = client
        return self._client

    async def _keychain_backend(self) -> KeychainBackend:
        if self._backend is None:
            self._backend = await get_backend()
        return self._backend

    async def _capture_rate_limit(self, response: httpx.Response) -> None:
        if response.status_code != _HTTP_TOO_MANY_REQUESTS:
            return
        retry_after_seconds = _parse_retry_after(response.headers.get("Retry-After"))
        _ = _RETRY_AFTER_SECONDS.set(retry_after_seconds)
        await record_429(self.account.api_type, self.account.alias, retry_after_seconds)

    def _auth_headers(self, tokens: AccountTokens) -> dict[str, str]:
        # Unlike Display, Business API requires Access-Token without an Authorization Bearer prefix.
        return {"Access-Token": tokens.access_token.get_secret_value()}

    def _has_refresh_token(self, tokens: AccountTokens) -> bool:
        return bool(tokens.refresh_token.get_secret_value())

    def _account_broken_error(self, business_error: BusinessApiError) -> AccountBrokenError:
        return AccountBrokenError(
            self.account.alias,
            status=self.account.status.value,
            context={
                "re_auth_required": True,
                "tiktok_code": business_error.tiktok_code,
                "request_id": business_error.request_id,
                "endpoint": business_error.context.get("endpoint"),
            },
        )


async def _refresh_lock_for(account: Account) -> asyncio.Lock:
    key = (account.api_type, account.sandbox, account.alias)
    async with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _REFRESH_LOCKS[key] = lock
        return lock


def _is_retryable_http_status(exc: BaseException) -> bool:
    if not isinstance(exc, SanitizedHttpxError):
        return False
    return exc.status == _HTTP_TOO_MANY_REQUESTS or 500 <= exc.status < 600


def _retry_wait_seconds(retry_state: RetryCallState) -> float:
    retry_after_seconds = _RETRY_AFTER_SECONDS.get()
    if retry_after_seconds is not None:
        return retry_after_seconds
    attempt_number = max(retry_state.attempt_number, 1)
    exponential_delay = 0.05 * (2 ** (attempt_number - 1))
    jitter = random.uniform(0.0, 0.05)
    return float(min(exponential_delay + jitter, 1.0))


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return max(float(stripped), 0.0)
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)


def _account_without_token_fields(account: Account | AccountWithTokens) -> Account:
    return Account(
        alias=account.alias,
        api_type=account.api_type,
        sandbox=account.sandbox,
        tiktok_id=account.tiktok_id,
        display_name=account.display_name,
        avatar_url=account.avatar_url,
        scopes=list(account.scopes),
        created_at=account.created_at,
        last_used_at=account.last_used_at,
        status=account.status,
    )


def _tokens_from_account(account: Account | AccountWithTokens) -> AccountTokens | None:
    if not isinstance(account, AccountWithTokens):
        return None
    return AccountTokens(
        access_token=account.access_token,
        refresh_token=account.refresh_token,
        access_token_expires_at=account.access_token_expires_at,
        refresh_token_expires_at=account.refresh_token_expires_at,
        last_rotated_at=account.last_rotated_at,
    )


def _register_tokens(tokens: AccountTokens) -> None:
    add_runtime_token(tokens.access_token.get_secret_value(), "access_token")
    add_runtime_token(tokens.refresh_token.get_secret_value(), "refresh_token")


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    msg = f"Business token refresh response is missing string field {key}."
    raise BusinessApiError(
        code=-1,
        message=msg,
        context={"endpoint": BusinessAPIClient.REFRESH_PATH},
    )


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Business token refresh response is missing integer field {key}."
        raise BusinessApiError(
            code=-1,
            message=msg,
            context={"endpoint": BusinessAPIClient.REFRESH_PATH},
        )
    return value


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Business token refresh response field {key} must be an integer."
        raise BusinessApiError(
            code=-1,
            message=msg,
            context={"endpoint": BusinessAPIClient.REFRESH_PATH},
        )
    return cast(int, value)


__all__ = ["BusinessAPIClient"]
