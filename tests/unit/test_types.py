"""Tests for core pydantic v2 types and structured errors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, SecretStr

from tiktok_mcp.types import (
    Account,
    AccountBrokenError,
    AccountNotFoundError,
    AccountStatus,
    AccountSummary,
    ApiType,
    AppCredentialsNotSetError,
    AppCredentialsSummary,
    AppCredentialsVerifyResult,
    BusinessApiError,
    DisplayApiError,
    KeychainLockedError,
    KeychainUnavailableError,
    OAuthAuthorizationUrl,
    OAuthHostMismatchError,
    OAuthInProgress,
    OAuthStateInvalidError,
    RateLimitedError,
    WritesDisabledError,
)
from tiktok_mcp.types.accounts import make_fingerprint
from tiktok_mcp.types.app_credentials import AppCredentials

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=10)
PUBLIC_MODEL_CASES = [
    pytest.param(
        Account,
        Account(
            alias="demo-display",
            api_type=ApiType.DISPLAY,
            sandbox=True,
            tiktok_id="abcd1234abcd1234abcd1234",
            display_name="Demo Display",
            avatar_url="https://example.com/avatar.png",
            scopes=["user.info.basic"],
            created_at=NOW,
            last_used_at=LATER,
            status=AccountStatus.OK,
        ),
        id="account",
    ),
    pytest.param(
        AccountSummary,
        AccountSummary.from_account(
            Account(
                alias="demo-display",
                api_type=ApiType.DISPLAY,
                sandbox=True,
                tiktok_id="abcd1234abcd1234abcd1234",
                display_name="Demo Display",
                avatar_url="https://example.com/avatar.png",
                scopes=["user.info.basic"],
                created_at=NOW,
                last_used_at=LATER,
                status=AccountStatus.OK,
            )
        ),
        id="account-summary",
    ),
    pytest.param(
        OAuthInProgress,
        OAuthInProgress(
            state="oauth-state-123",
            api_type=ApiType.MARKETING,
            pkce_verifier="pkce-verifier",
            suggested_alias="no-marketing-abc1",
            expires_at=LATER,
            created_at=NOW,
        ),
        id="oauth-in-progress",
    ),
    pytest.param(
        OAuthAuthorizationUrl,
        OAuthAuthorizationUrl(
            url="https://www.tiktok.com/auth?state=oauth-state-123",
            state="oauth-state-123",
            suggested_alias="no-marketing-abc1",
            expires_at=LATER,
        ),
        id="oauth-authorization-url",
    ),
    pytest.param(
        AppCredentialsSummary,
        AppCredentialsSummary.from_credentials(
            AppCredentials(
                api_type=ApiType.CONTENT_POSTING,
                sandbox=False,
                client_id=SecretStr("client-id-marker-1"),
                client_secret=SecretStr("client-secret-marker"),
                created_at=NOW,
            ),
            registered_redirect_uri="http://localhost:8765/callback",
        ),
        id="app-credentials-summary",
    ),
    pytest.param(
        AppCredentialsVerifyResult,
        AppCredentialsVerifyResult(
            api_type=ApiType.BUSINESS_ORGANIC,
            sandbox=True,
            client_id_fingerprint="orga...len=18",
            valid=False,
            verified_at=NOW,
            error_code="invalid_client",
            error_message="TikTok rejected the sandbox credentials",
        ),
        id="app-credentials-verify-result",
    ),
]


@pytest.mark.parametrize(("model_type", "model_instance"), PUBLIC_MODEL_CASES)
def test_public_model_json_roundtrip(
    model_type: type[BaseModel], model_instance: BaseModel
) -> None:
    """Public models preserve values through JSON serialization and validation."""
    serialized_json = model_instance.model_dump_json()
    roundtrip_model = model_type.model_validate_json(serialized_json)

    assert roundtrip_model == model_instance


def test_secret_masking() -> None:
    """SecretStr credentials are masked in JSON output and never leak literals."""
    app_credentials = AppCredentials(
        api_type=ApiType.DISPLAY,
        sandbox=True,
        client_id=SecretStr("display-client-id-marker"),
        client_secret=SecretStr("hunter2-secret-marker"),
        created_at=NOW,
    )

    credentials_json = app_credentials.model_dump_json()
    credentials_summary_json = AppCredentialsSummary.from_credentials(
        app_credentials
    ).model_dump_json()

    assert "hunter2-secret-marker" not in credentials_json
    assert "display-client-id-marker" not in credentials_json
    assert "hunter2-secret-marker" not in credentials_summary_json
    assert "display-client-id-marker" not in credentials_summary_json
    assert "**********" in credentials_json


def test_error_serialization() -> None:
    """Typed errors serialize to the structured error envelope shape."""
    business_error = BusinessApiError(
        code=40001,
        message="TikTok rejected the request",
        request_id="req-123",
        context={"endpoint": "/open_api/v1.3/campaign/get"},
    )
    typed_errors = [
        WritesDisabledError("delete_campaign", "marketing", "would have deleted campaign 7234"),
        KeychainLockedError(context={"operation": "read_token"}),
        KeychainUnavailableError("No secure backend"),
        AccountNotFoundError("demo-display", api_type="display", sandbox=True),
        AccountBrokenError("demo-display", status="broken"),
        AppCredentialsNotSetError("marketing", sandbox=False),
        OAuthStateInvalidError("expired", context={"state": "oauth-state-123"}),
        OAuthHostMismatchError("oauth.example.com", "evil.example"),
        business_error,
        DisplayApiError(401, "Display API rejected the request", "access_token_invalid"),
        RateLimitedError(12.5, 3),
    ]

    for typed_error in typed_errors:
        error_envelope = typed_error.to_dict()
        assert set(error_envelope) == {"error", "message", "context"}
        assert isinstance(error_envelope["error"], str)
        assert isinstance(error_envelope["message"], str)
        assert isinstance(error_envelope["context"], dict)

    assert typed_errors[0].to_dict()["context"]["tool"] == "delete_campaign"
    assert typed_errors[6].to_dict()["context"]["reason"] == "expired"
    assert business_error.to_dict()["context"]["tiktok_code"] == 40001
    assert business_error.tiktok_code == 40001
    assert business_error.request_id == "req-123"
    assert typed_errors[10].to_dict()["context"]["attempts"] == 3


def test_account_summary_fingerprint_shape() -> None:
    """Account summaries expose only the tiktok_id fingerprint shape."""
    raw_tiktok_id = "abcd1234abcd1234abcd1234"
    account_summary = AccountSummary(
        alias="demo-display",
        api_type=ApiType.DISPLAY,
        sandbox=True,
        tiktok_id_fingerprint=make_fingerprint(raw_tiktok_id),
        display_name="Demo Display",
        avatar_url=None,
        scopes=["user.info.basic"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
    )

    summary_dump = account_summary.model_dump()
    summary_json = account_summary.model_dump_json()

    assert account_summary.tiktok_id_fingerprint == "abcd...len=24"
    assert "tiktok_id" not in summary_dump
    assert raw_tiktok_id not in summary_json
