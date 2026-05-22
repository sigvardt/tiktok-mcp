from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from tiktok_mcp.types.accounts import AccountTokens


def test_account_tokens_no_refresh() -> None:
    tokens = AccountTokens(
        access_token=SecretStr("access-token"),
        refresh_token=None,
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert tokens.refresh_token is None
    assert tokens.refresh_token_expires_at is None
