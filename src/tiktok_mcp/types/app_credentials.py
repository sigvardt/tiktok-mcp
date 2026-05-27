"""App credential models for TikTok API integrations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .accounts import ApiType, make_fingerprint

STRICT_MODEL_CONFIG = ConfigDict(frozen=False, extra="forbid")


def utc_now() -> datetime:
    return datetime.now(UTC)


class AppCredentials(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG
    api_type: ApiType
    sandbox: bool
    client_id: SecretStr
    client_secret: SecretStr
    created_at: datetime = Field(default_factory=utc_now)


class AppCredentialsSummary(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG
    api_type: ApiType
    sandbox: bool
    client_id_fingerprint: str
    client_secret_set: bool
    created_at: datetime
    registered_redirect_uri: str | None = None

    @classmethod
    def from_credentials(
        cls,
        app_credentials: AppCredentials,
        *,
        registered_redirect_uri: str | None = None,
    ) -> AppCredentialsSummary:
        return cls(
            api_type=app_credentials.api_type,
            sandbox=app_credentials.sandbox,
            client_id_fingerprint=make_fingerprint(app_credentials.client_id.get_secret_value()),
            client_secret_set=bool(app_credentials.client_secret.get_secret_value()),
            created_at=app_credentials.created_at,
            registered_redirect_uri=registered_redirect_uri,
        )


class AppCredentialsVerifyResult(BaseModel):
    model_config: ClassVar[ConfigDict] = STRICT_MODEL_CONFIG
    api_type: ApiType
    sandbox: bool
    client_id_fingerprint: str
    valid: bool
    registered_redirect_uri: str | None = None
    verified_at: datetime = Field(default_factory=utc_now)
    error_code: str | None = None
    error_message: str | None = None
