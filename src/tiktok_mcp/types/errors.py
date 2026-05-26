"""Typed error envelopes for TikTok API responses."""

from __future__ import annotations

from typing import Any, Literal

ErrorContext = dict[str, Any]
ErrorEnvelope = dict[str, Any]
OAuthInvalidReason = Literal["unknown", "expired", "consumed", "replay"]


def _merge_context(base_context: ErrorContext, extra_context: ErrorContext | None) -> ErrorContext:
    if extra_context is None:
        return base_context
    return {**base_context, **extra_context}


class TikTokMCPError(Exception):
    code: str
    message: str
    context: ErrorContext

    def __init__(self, code: str, message: str, context: ErrorContext | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}

    def to_dict(self) -> ErrorEnvelope:
        return {"error": self.code, "message": self.message, "context": self.context}


class WritesDisabledError(TikTokMCPError):
    tool: str
    api: str
    would_have_done: str

    def __init__(
        self,
        tool: str,
        api: str,
        would_have_done: str,
        *,
        context: ErrorContext | None = None,
    ) -> None:
        self.tool = tool
        self.api = api
        self.would_have_done = would_have_done
        message = (
            f"Write/delete tools for '{api}' are disabled. "
            f"Set TIKTOK_MCP_ALLOW_WRITES=all (or include '{api}') to enable."
        )
        super().__init__(
            code="writes_disabled",
            message=message,
            context=_merge_context(
                {"tool": tool, "api": api, "would_have_done": would_have_done}, context
            ),
        )


class KeychainLockedError(TikTokMCPError):
    def __init__(self, *, context: ErrorContext | None = None) -> None:
        super().__init__(
            code="keychain_locked",
            message="Keychain is locked.",
            context=context,
        )


class KeychainUnavailableError(TikTokMCPError):
    reason: str | None

    def __init__(self, reason: str | None = None, *, context: ErrorContext | None = None) -> None:
        self.reason = reason
        keychain_context: ErrorContext = {}
        if reason is not None:
            keychain_context["reason"] = reason
        super().__init__(
            code="keychain_unavailable",
            message="Keychain is unavailable.",
            context=_merge_context(keychain_context, context),
        )


class AccountNotFoundError(TikTokMCPError):
    alias: str
    api_type: str | None
    sandbox: bool | None

    def __init__(
        self,
        alias: str,
        *,
        api_type: str | None = None,
        sandbox: bool | None = None,
        context: ErrorContext | None = None,
    ) -> None:
        self.alias = alias
        self.api_type = api_type
        self.sandbox = sandbox
        account_context: ErrorContext = {"alias": alias}
        if api_type is not None:
            account_context["api_type"] = api_type
        if sandbox is not None:
            account_context["sandbox"] = sandbox
        super().__init__(
            code="account_not_found",
            message=f"Account '{alias}' was not found.",
            context=_merge_context(account_context, context),
        )


class AccountBrokenError(TikTokMCPError):
    alias: str
    status: str | None

    def __init__(
        self,
        alias: str,
        *,
        status: str | None = None,
        context: ErrorContext | None = None,
    ) -> None:
        self.alias = alias
        self.status = status
        account_context: ErrorContext = {"alias": alias}
        if status is not None:
            account_context["status"] = status
        super().__init__(
            code="account_broken",
            message=f"Account '{alias}' is broken and must be repaired.",
            context=_merge_context(account_context, context),
        )


class AppCredentialsNotSetError(TikTokMCPError):
    api_type: str
    sandbox: bool

    def __init__(
        self,
        api_type: str,
        sandbox: bool,
        *,
        context: ErrorContext | None = None,
    ) -> None:
        self.api_type = api_type
        self.sandbox = sandbox
        mode = "sandbox" if sandbox else "production"
        super().__init__(
            code="app_credentials_not_set",
            message=f"App credentials are not set for {api_type} ({mode}).",
            context=_merge_context({"api_type": api_type, "sandbox": sandbox}, context),
        )


class OAuthStateInvalidError(TikTokMCPError):
    reason: OAuthInvalidReason

    def __init__(
        self,
        reason: OAuthInvalidReason,
        *,
        context: ErrorContext | None = None,
    ) -> None:
        self.reason = reason
        super().__init__(
            code="oauth_state_invalid",
            message=f"OAuth state invalid (reason: {reason})",
            context=_merge_context({"reason": reason}, context),
        )


class OAuthHostMismatchError(TikTokMCPError):
    expected_host: str
    actual_host: str

    def __init__(
        self,
        expected_host: str,
        actual_host: str,
        *,
        context: ErrorContext | None = None,
    ) -> None:
        self.expected_host = expected_host
        self.actual_host = actual_host
        super().__init__(
            code="oauth_host_mismatch",
            message=f"OAuth redirect host mismatch: expected {expected_host}, got {actual_host}.",
            context=_merge_context(
                {"expected_host": expected_host, "actual_host": actual_host}, context
            ),
        )


class BusinessApiError(TikTokMCPError):
    tiktok_code: int
    request_id: str | None

    def __init__(
        self,
        code: int,
        message: str,
        request_id: str | None = None,
        context: ErrorContext | None = None,
    ) -> None:
        self.tiktok_code = code
        self.request_id = request_id
        super().__init__(
            code="business_api_error",
            message=message,
            context={"tiktok_code": code, "request_id": request_id, **(context or {})},
        )


class DisplayApiError(TikTokMCPError):
    http_status: int
    error_code: str | None

    def __init__(
        self,
        http_status: int,
        message: str,
        error_code: str | None = None,
        *,
        context: ErrorContext | None = None,
    ) -> None:
        self.http_status = http_status
        self.error_code = error_code
        super().__init__(
            code="display_api_error",
            message=message,
            context=_merge_context({"http_status": http_status, "error_code": error_code}, context),
        )


class RateLimitedError(TikTokMCPError):
    retry_after: float | None
    attempts: int

    def __init__(
        self,
        retry_after: float | None,
        attempts: int,
        *,
        message: str = "TikTok API rate limit exceeded.",
        context: ErrorContext | None = None,
    ) -> None:
        self.retry_after = retry_after
        self.attempts = attempts
        super().__init__(
            code="rate_limited",
            message=message,
            context=_merge_context({"retry_after": retry_after, "attempts": attempts}, context),
        )
