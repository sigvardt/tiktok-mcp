# pyright: reportMissingTypeStubs=false, reportAny=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false, reportExplicitAny=false
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import vcr
from pydantic import SecretStr

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.tools import marketing_writes_campaigns as campaign_tools
from tiktok_mcp.tools.marketing_writes_campaigns import (
    create_campaign,
    delete_campaign,
    update_campaign,
    update_campaign_status,
)
from tiktok_mcp.types.accounts import AccountStatus, AccountWithTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials

ALIAS = "marketing-demo"
ADVERTISER_ID = "7642629596042543111"
CAMPAIGN_ID = "1733456789012345"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes"
CAMPAIGNS_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    filter_headers=[("Access-Token", "REDACTED")],
)


@pytest.mark.asyncio
async def test_campaign_crud_replays_marketing_campaign_cassettes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vcr_client(monkeypatch)
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "")

    with CAMPAIGNS_VCR.use_cassette("marketing_campaigns/create_traffic.yaml", record_mode="none"):
        created = await create_campaign(
            ALIAS,
            ADVERTISER_ID,
            "QA-TEST",
            "TRAFFIC",
            "BUDGET_MODE_DAY",
            50,
        )
    with CAMPAIGNS_VCR.use_cassette("marketing_campaigns/update_name.yaml", record_mode="none"):
        updated = await update_campaign(
            ALIAS,
            ADVERTISER_ID,
            CAMPAIGN_ID,
            campaign_name="QA-UPDATED",
        )
    with CAMPAIGNS_VCR.use_cassette("marketing_campaigns/status_disable.yaml", record_mode="none"):
        disabled = await update_campaign_status(ALIAS, ADVERTISER_ID, [CAMPAIGN_ID], "DISABLE")
    with CAMPAIGNS_VCR.use_cassette("marketing_campaigns/delete.yaml", record_mode="none"):
        deleted = await delete_campaign(ALIAS, ADVERTISER_ID, [CAMPAIGN_ID])

    assert created == {
        "campaign_id": CAMPAIGN_ID,
        "modify_time": "2026-05-22 12:00:00",
        "status": "ENABLE",
    }
    assert updated == {
        "campaign_id": CAMPAIGN_ID,
        "modify_time": "2026-05-22 12:10:00",
        "status": "ENABLE",
    }
    assert disabled == {
        "campaign_id": CAMPAIGN_ID,
        "modify_time": "2026-05-22 12:20:00",
        "status": "DISABLE",
    }
    assert deleted == {
        "campaign_id": CAMPAIGN_ID,
        "modify_time": "2026-05-22 12:30:00",
        "status": "DELETE",
    }
    for cassette_path in (CASSETTE_DIR / "marketing_campaigns").glob("*.yaml"):
        cassette_text = cassette_path.read_text(encoding="utf-8")
        assert "marketing-access-token" not in cassette_text
        assert "REDACTED" in cassette_text


def _install_vcr_client(monkeypatch: pytest.MonkeyPatch) -> None:
    async def build_client(alias: str) -> BusinessAPIClient:
        assert alias == ALIAS
        return BusinessAPIClient(
            _account(),
            _credentials(),
        )

    monkeypatch.setattr(campaign_tools, "_build_business_client", build_client)


def _account() -> AccountWithTokens:
    return AccountWithTokens(
        alias=ALIAS,
        api_type=ApiType.MARKETING,
        sandbox=True,
        tiktok_id=ADVERTISER_ID,
        display_name="Marketing Demo",
        avatar_url=None,
        scopes=["business.campaign.write"],
        created_at=NOW,
        last_used_at=None,
        status=AccountStatus.OK,
        access_token=SecretStr("marketing-access-token"),
        refresh_token=SecretStr("marketing-refresh-token"),
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_expires_at=NOW + timedelta(days=30),
        last_rotated_at=NOW,
    )


def _credentials() -> AppCredentials:
    return AppCredentials(
        api_type=ApiType.MARKETING,
        sandbox=True,
        client_id=SecretStr("marketing-client-id"),
        client_secret=SecretStr("marketing-client-secret"),
        created_at=NOW,
    )
