"""OAuth state and token models for TikTok login flows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .accounts import ApiType

STRICT_MODEL_CONFIG = ConfigDict(frozen=False, extra="forbid")
TOKEN_RESPONSE_CONFIG = ConfigDict(frozen=False, extra="allow")


def utc_now() -> datetime:
    return datetime.now(UTC)


class OAuthInProgress(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG

    state: str
    api_type: ApiType
    pkce_verifier: str | None = None
    suggested_alias: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)


class OAuthAuthorizationUrl(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG

    url: str
    state: str
    suggested_alias: str
    expires_at: datetime


class OAuthTokenResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = TOKEN_RESPONSE_CONFIG
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_in: int
    scope: list[str]
    token_type: str = "Bearer"
    open_id: str | None = None
    advertiser_ids: list[str] | None = None
