from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from tiktok_mcp.tools.accounts import (
    _build_tiktok_pkce_challenge,
    _new_pkce_verifier,
    build_rfc7636_pkce_challenge,
)
from tiktok_mcp.types.accounts import AccountTokens


def test_account_tokens_no_refresh() -> None:
    tokens = AccountTokens(
        access_token=SecretStr("access-token"),
        refresh_token=None,
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert tokens.refresh_token is None
    assert tokens.refresh_token_expires_at is None


def test_pkce_challenge_matches_rfc7636_appendix_b_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = build_rfc7636_pkce_challenge(verifier)

    assert challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_challenge_matches_rfc7636_s256_formula() -> None:
    verifier = "A" * 43
    challenge = build_rfc7636_pkce_challenge(verifier)
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )

    assert 43 <= len(verifier) <= 128
    assert 43 <= len(_new_pkce_verifier()) <= 128
    assert challenge == expected


def test_tiktok_desktop_pkce_challenge_uses_hex_sha256() -> None:
    verifier = "A" * 43
    challenge = _build_tiktok_pkce_challenge(verifier)

    assert challenge == hashlib.sha256(verifier.encode("ascii")).hexdigest()
    assert len(challenge) == 64


def test_tiktok_pkce_verifier_matches_desktop_constraints() -> None:
    verifier = _new_pkce_verifier()
    allowed_characters = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    assert len(verifier) == 64
    assert set(verifier) <= allowed_characters
