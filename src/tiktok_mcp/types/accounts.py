"""Account models for TikTok auth flows."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr

STRICT_MODEL_CONFIG = ConfigDict(frozen=False, extra="forbid")
MARKETING_DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 24 * 60 * 60


def utc_now() -> datetime:
    return datetime.now(UTC)


def make_fingerprint(value: str) -> str:
    return f"{value[:4]}...len={len(value)}"


class ApiType(str, Enum):  # noqa: UP042
    DISPLAY = "display"
    MARKETING = "marketing"
    BUSINESS_ORGANIC = "business_organic"
    CONTENT_POSTING = "content_posting"


class AccountStatus(str, Enum):  # noqa: UP042
    OK = "ok"
    BROKEN = "broken"
    REFRESH_PENDING = "refresh_pending"
    REVOKED = "revoked"


class Account(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG

    alias: str = Field(pattern=r"^[a-z0-9-]{3,50}$")
    api_type: ApiType
    sandbox: bool
    tiktok_id: str
    display_name: str | None = None
    avatar_url: str | None = None
    scopes: list[str]
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None
    status: AccountStatus


class AccountTokens(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None = None
    last_rotated_at: datetime = Field(default_factory=utc_now)


class AccountWithTokens(Account):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None = None
    last_rotated_at: datetime = Field(default_factory=utc_now)


class AccountSummary(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG

    alias: str = Field(pattern=r"^[a-z0-9-]{3,50}$")
    api_type: ApiType
    sandbox: bool
    tiktok_id_fingerprint: str
    display_name: str | None = None
    avatar_url: str | None = None
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None
    status: AccountStatus

    @classmethod
    def from_account(cls, account: Account) -> AccountSummary:
        return cls(
            alias=account.alias,
            api_type=account.api_type,
            sandbox=account.sandbox,
            tiktok_id_fingerprint=make_fingerprint(account.tiktok_id),
            display_name=account.display_name,
            avatar_url=account.avatar_url,
            scopes=account.scopes,
            created_at=account.created_at,
            last_used_at=account.last_used_at,
            status=account.status,
        )
