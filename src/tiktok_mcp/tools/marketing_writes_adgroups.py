from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import json
from typing import ClassVar, Literal, Self, cast

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.auth.keychain import (
    KeychainBackend,
    account_key,
    app_creds_key,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.auth.redactor import register_token
from tiktok_mcp.decorators import require_writes_enabled
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountNotFoundError,
    AppCredentialsNotSetError,
    KeychainUnavailableError,
)

ADGROUP_CREATE_PATH = "/open_api/v1.3/adgroup/create/"
ADGROUP_UPDATE_PATH = "/open_api/v1.3/adgroup/update/"
ADGROUP_STATUS_UPDATE_PATH = "/open_api/v1.3/adgroup/status/update/"
ADGROUP_DELETE_PATH = "/open_api/v1.3/adgroup/delete/"

OperationStatus = Literal["ENABLE", "DISABLE", "DELETE"]
JsonObject = dict[str, object]


class TargetingBlock(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    location_ids: list[str] | None = Field(default=None, min_length=1)
    zipcode_ids: list[str] | None = Field(default=None, min_length=1)
    genders: list[str] | None = None
    age_groups: list[str] | None = None
    languages: list[str] | None = None
    interests: list[str] | None = None
    behaviors: list[str] | None = None
    operating_systems: list[str] | None = None
    network_types: list[str] | None = None


class CreateAdGroupRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    adgroup_name: str = Field(min_length=1)
    placement_type: str = Field(min_length=1)
    schedule_type: str = Field(min_length=1)
    schedule_start_time: str = Field(min_length=1)
    billing_event: str = Field(min_length=1)
    optimization_goal: str = Field(min_length=1)
    bid_type: str = Field(min_length=1)
    budget_mode: str = Field(min_length=1)
    budget: float = Field(gt=0)
    targeting: TargetingBlock
    promotion_type: str | None = Field(default=None, min_length=1)
    schedule_end_time: str | None = Field(default=None, min_length=1)
    bid_price: float | None = Field(default=None, ge=0)
    audience_ids: list[str] | None = None

    @model_validator(mode="after")
    def require_schedule_end_for_start_end(self) -> Self:
        if self.schedule_type == "SCHEDULE_START_END" and self.schedule_end_time is None:
            raise ValueError(
                "schedule_end_time is required when schedule_type is SCHEDULE_START_END"
            )
        if self.targeting.location_ids is None and self.targeting.zipcode_ids is None:
            raise ValueError("targeting requires location_ids or zipcode_ids")
        return self


class UpdateAdGroupRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    adgroup_id: str = Field(min_length=1)
    adgroup_name: str | None = Field(default=None, min_length=1)
    placement_type: str | None = Field(default=None, min_length=1)
    schedule_type: str | None = Field(default=None, min_length=1)
    billing_event: str | None = Field(default=None, min_length=1)
    optimization_goal: str | None = Field(default=None, min_length=1)
    bid_type: str | None = Field(default=None, min_length=1)
    budget: float | None = Field(default=None, gt=0)
    targeting: TargetingBlock | None = None
    bid_price: float | None = Field(default=None, ge=0)
    audience_ids: list[str] | None = None

    @model_validator(mode="after")
    def require_one_update_field(self) -> Self:
        if any(
            value is not None
            for value in (
                self.adgroup_name,
                self.placement_type,
                self.schedule_type,
                self.billing_event,
                self.optimization_goal,
                self.bid_type,
                self.budget,
                self.targeting,
                self.bid_price,
                self.audience_ids,
            )
        ):
            return self
        raise ValueError("update_adgroup requires at least one mutable adgroup field")


class UpdateAdGroupStatusRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    adgroup_ids: list[str] = Field(min_length=1)
    operation_status: OperationStatus


class DeleteAdGroupRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    adgroup_ids: list[str] = Field(min_length=1)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def create_adgroup(
    alias: str,
    advertiser_id: str,
    campaign_id: str,
    adgroup_name: str,
    placement_type: str,
    schedule_type: str,
    schedule_start_time: str,
    billing_event: str,
    optimization_goal: str,
    bid_type: str,
    budget_mode: str,
    budget: float,
    targeting: dict[str, object],
    promotion_type: str | None = None,
    schedule_end_time: str | None = None,
    bid_price: float | None = None,
    audience_ids: list[str] | None = None,
) -> JsonObject:
    try:
        params = CreateAdGroupRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "adgroup_name": adgroup_name,
                "placement_type": placement_type,
                "schedule_type": schedule_type,
                "schedule_start_time": schedule_start_time,
                "billing_event": billing_event,
                "optimization_goal": optimization_goal,
                "bid_type": bid_type,
                "budget_mode": budget_mode,
                "budget": budget,
                "targeting": targeting,
                "promotion_type": promotion_type,
                "schedule_end_time": schedule_end_time,
                "bid_price": bid_price,
                "audience_ids": audience_ids,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_adgroup_payload(
        params.alias,
        ADGROUP_CREATE_PATH,
        _adgroup_payload(params, exclude={"alias"}),
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def update_adgroup(
    alias: str,
    advertiser_id: str,
    adgroup_id: str,
    adgroup_name: str | None = None,
    placement_type: str | None = None,
    schedule_type: str | None = None,
    billing_event: str | None = None,
    optimization_goal: str | None = None,
    bid_type: str | None = None,
    budget: float | None = None,
    targeting: dict[str, object] | None = None,
    bid_price: float | None = None,
    audience_ids: list[str] | None = None,
) -> JsonObject:
    try:
        params = UpdateAdGroupRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "adgroup_id": adgroup_id,
                "adgroup_name": adgroup_name,
                "placement_type": placement_type,
                "schedule_type": schedule_type,
                "billing_event": billing_event,
                "optimization_goal": optimization_goal,
                "bid_type": bid_type,
                "budget": budget,
                "targeting": targeting,
                "bid_price": bid_price,
                "audience_ids": audience_ids,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_adgroup_payload(
        params.alias,
        ADGROUP_UPDATE_PATH,
        _adgroup_payload(params, exclude={"alias"}),
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def update_adgroup_status(
    alias: str,
    advertiser_id: str,
    adgroup_ids: list[str],
    operation_status: OperationStatus,
) -> JsonObject:
    try:
        params = UpdateAdGroupStatusRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "adgroup_ids": adgroup_ids,
                "operation_status": operation_status,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_adgroup_payload(
        params.alias,
        ADGROUP_STATUS_UPDATE_PATH,
        _payload_from_model(params, exclude={"alias"}),
    )


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def delete_adgroup(
    alias: str,
    advertiser_id: str,
    adgroup_ids: list[str],
) -> JsonObject:
    try:
        params = DeleteAdGroupRequest.model_validate(
            {"alias": alias, "advertiser_id": advertiser_id, "adgroup_ids": adgroup_ids}
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_adgroup_payload(
        params.alias,
        ADGROUP_DELETE_PATH,
        _payload_from_model(params, exclude={"alias"}),
    )


async def _post_adgroup_payload(alias: str, path: str, payload: JsonObject) -> JsonObject:
    async with await _build_business_client(alias) as client:
        response = await client.request("POST", path, json=payload)
    return _raw_payload(response)


def _payload_from_model(model: BaseModel, *, exclude: set[str]) -> JsonObject:
    return cast(JsonObject, model.model_dump(mode="json", exclude=exclude, exclude_none=True))


def _adgroup_payload(model: BaseModel, *, exclude: set[str]) -> JsonObject:
    payload = _payload_from_model(model, exclude=exclude)
    targeting = payload.pop("targeting", None)
    if isinstance(targeting, dict):
        payload.update(cast(dict[str, object], targeting))
    return payload


def _validation_error(exc: ValidationError) -> JsonObject:
    return {
        "error": "validation_error",
        "message": "AdGroup write input validation failed.",
        "details": exc.errors(include_url=False),
    }


def _raw_payload(payload: object) -> JsonObject:
    if isinstance(payload, dict):
        return cast(JsonObject, payload)
    return {}


async def _build_business_client(alias: str) -> BusinessAPIClient:
    backend = await get_backend()
    account, tokens = await _load_marketing_account(backend, alias)
    credentials = await _load_app_credentials(backend, account)
    return BusinessAPIClient(account, credentials, tokens=tokens, backend=backend)


async def _load_marketing_account(
    backend: KeychainBackend,
    alias: str,
) -> tuple[Account, AccountTokens]:
    for sandbox in (False, True):
        raw_record = await backend.get(account_key(ApiType.MARKETING, sandbox, alias))
        if raw_record is None:
            continue
        return deserialize_account_record(raw_record)
    raise AccountNotFoundError(alias, api_type=ApiType.MARKETING.value)


async def _load_app_credentials(
    backend: KeychainBackend,
    account: Account,
) -> AppCredentials:
    raw_credentials = await backend.get(app_creds_key(account.api_type, account.sandbox))
    if raw_credentials is None:
        raise AppCredentialsNotSetError(account.api_type.value, account.sandbox)
    try:
        payload = cast(object, json.loads(raw_credentials))
        credentials = AppCredentials.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise KeychainUnavailableError(
            "Stored Marketing app credentials are invalid.",
            context={"api_type": account.api_type.value, "sandbox": account.sandbox},
        ) from exc
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")
    return credentials


__all__ = [
    "ADGROUP_CREATE_PATH",
    "ADGROUP_DELETE_PATH",
    "ADGROUP_STATUS_UPDATE_PATH",
    "ADGROUP_UPDATE_PATH",
    "CreateAdGroupRequest",
    "DeleteAdGroupRequest",
    "TargetingBlock",
    "UpdateAdGroupRequest",
    "UpdateAdGroupStatusRequest",
    "create_adgroup",
    "delete_adgroup",
    "update_adgroup",
    "update_adgroup_status",
]
