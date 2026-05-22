from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class Advertiser(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    advertiser_id: str | None = None
    advertiser_name: str | None = None
    advertiser_role: str | None = None
    advertiser_account_type: str | None = None
    bc_id: str | None = None
    currency: str | None = None
    status: str | None = None
    timezone: str | None = None


class AdvertiserInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    advertiser_id: str | None = None
    advertiser_name: str | None = None
    advertiser_type: str | None = None
    company: str | None = None
    country: str | None = None
    currency: str | None = None
    description: str | None = None
    industry: str | None = None
    license_no: str | None = None
    rejection_reason: str | None = None
    status: str | None = None
    timezone: str | None = None


class BusinessCenter(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    bc_id: str | None = None
    bc_name: str | None = None
    bc_status: str | None = None
    company: str | None = None
    country_code: str | None = None
    currency: str | None = None
    timezone: str | None = None


class Campaign(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    advertiser_id: str | None = None
    app_id: str | None = None
    budget: float | None = None
    budget_mode: str | None = None
    campaign_id: str | None = None
    campaign_name: str | None = None
    campaign_type: str | None = None
    create_time: str | None = None
    modify_time: str | None = None
    objective_type: str | None = None
    operation_status: str | None = None
    secondary_status: str | None = None


class AdGroup(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    adgroup_id: str | None = None
    adgroup_name: str | None = None
    advertiser_id: str | None = None
    bid_type: str | None = None
    billing_event: str | None = None
    budget: float | None = None
    budget_mode: str | None = None
    campaign_id: str | None = None
    create_time: str | None = None
    modify_time: str | None = None
    operation_status: str | None = None
    optimization_goal: str | None = None
    placement_type: str | None = None
    promotion_type: str | None = None
    schedule_end_time: str | None = None
    schedule_start_time: str | None = None
    secondary_status: str | None = None


class Ad(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    ad_id: str | None = None
    ad_name: str | None = None
    ad_text: str | None = None
    adgroup_id: str | None = None
    advertiser_id: str | None = None
    call_to_action: str | None = None
    campaign_id: str | None = None
    create_time: str | None = None
    creative_type: str | None = None
    modify_time: str | None = None
    operation_status: str | None = None
    secondary_status: str | None = None


__all__ = [
    "Ad",
    "AdGroup",
    "Advertiser",
    "AdvertiserInfo",
    "BusinessCenter",
    "Campaign",
]
