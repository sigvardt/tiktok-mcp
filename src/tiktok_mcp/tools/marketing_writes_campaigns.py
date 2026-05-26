# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from typing import ClassVar, Literal, ParamSpec, Self, TypeVar, cast

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

JsonObject = dict[str, object]
CampaignWriteResult = dict[str, object]
OperationStatus = Literal["ENABLE", "DISABLE", "DELETE"]
ToolParams = ParamSpec("ToolParams")
ToolReturnT = TypeVar("ToolReturnT")

CAMPAIGN_CREATE_PATH = "/open_api/v1.3/campaign/create/"
CAMPAIGN_UPDATE_PATH = "/open_api/v1.3/campaign/update/"
CAMPAIGN_STATUS_UPDATE_PATH = "/open_api/v1.3/campaign/status/update/"
CAMPAIGN_DELETE_PATH = CAMPAIGN_STATUS_UPDATE_PATH

logger = logging.getLogger(__name__)


class CreateCampaignRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    campaign_name: str = Field(min_length=1)
    objective_type: str = Field(min_length=1)
    budget_mode: str = Field(min_length=1)
    budget: float = Field(gt=0)
    app_promotion_type: str | None = Field(default=None, min_length=1)
    special_industries: list[str] | None = None


class UpdateCampaignRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    campaign_name: str | None = Field(default=None, min_length=1)
    budget_mode: str | None = Field(default=None, min_length=1)
    budget: float | None = Field(default=None, gt=0)
    app_promotion_type: str | None = Field(default=None, min_length=1)
    special_industries: list[str] | None = None

    @model_validator(mode="after")
    def validate_has_write_fields(self) -> Self:
        if any(
            value is not None
            for value in (
                self.campaign_name,
                self.budget_mode,
                self.budget,
                self.app_promotion_type,
                self.special_industries,
            )
        ):
            return self
        raise ValueError("update_campaign requires at least one writable field")


class CampaignStatusUpdate(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    campaign_ids: list[str] = Field(min_length=1, max_length=1)
    operation_status: OperationStatus


class DeleteCampaignRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    campaign_ids: list[str] = Field(min_length=1, max_length=1)


def _log_blocked_write(
    action: str,
    endpoint: str,
) -> Callable[
    [Callable[ToolParams, Awaitable[ToolReturnT]]],
    Callable[ToolParams, Awaitable[ToolReturnT]],
]:
    def decorator(
        fn: Callable[ToolParams, Awaitable[ToolReturnT]],
    ) -> Callable[ToolParams, Awaitable[ToolReturnT]]:
        @wraps(fn)
        async def wrapper(
            *args: ToolParams.args,
            **kwargs: ToolParams.kwargs,
        ) -> ToolReturnT:
            result = await fn(*args, **kwargs)
            if not (isinstance(result, dict) and result.get("error") == "writes_disabled"):
                return result

            blocked_result = dict(result)
            would_have_done = _would_have_done(action, endpoint, args, kwargs)
            blocked_result["would_have_done"] = would_have_done
            logger.info(
                "TikTok Marketing campaign write blocked",
                extra={
                    "action": action,
                    "advertiser_id": would_have_done.get("advertiser_id"),
                    "campaign_id": would_have_done.get("campaign_id"),
                    "request_id": None,
                    "would_have_done": would_have_done,
                },
            )
            return cast(ToolReturnT, blocked_result)

        return wrapper

    return decorator


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@_log_blocked_write("campaign.create", CAMPAIGN_CREATE_PATH)
@require_writes_enabled("marketing")
async def create_campaign(
    alias: str,
    advertiser_id: str,
    campaign_name: str,
    objective_type: str,
    budget_mode: str,
    budget: float,
    app_promotion_type: str | None = None,
    special_industries: list[str] | None = None,
) -> CampaignWriteResult | JsonObject:
    try:
        params = CreateCampaignRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "campaign_name": campaign_name,
                "objective_type": objective_type,
                "budget_mode": budget_mode,
                "budget": budget,
                "app_promotion_type": app_promotion_type,
                "special_industries": special_industries,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    payload = await _post_campaign_write(
        params.alias,
        CAMPAIGN_CREATE_PATH,
        _request_payload(params, exclude={"alias"}),
    )
    result = _campaign_result(payload, fallback_campaign_id=None, fallback_status="ENABLE")
    _log_success("campaign.create", params.advertiser_id, result, payload)
    return result


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@_log_blocked_write("campaign.update", CAMPAIGN_UPDATE_PATH)
@require_writes_enabled("marketing")
async def update_campaign(
    alias: str,
    advertiser_id: str,
    campaign_id: str,
    campaign_name: str | None = None,
    budget_mode: str | None = None,
    budget: float | None = None,
    app_promotion_type: str | None = None,
    special_industries: list[str] | None = None,
) -> CampaignWriteResult | JsonObject:
    try:
        params = UpdateCampaignRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "budget_mode": budget_mode,
                "budget": budget,
                "app_promotion_type": app_promotion_type,
                "special_industries": special_industries,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    payload = await _post_campaign_write(
        params.alias,
        CAMPAIGN_UPDATE_PATH,
        _request_payload(params, exclude={"alias"}),
    )
    result = _campaign_result(
        payload,
        fallback_campaign_id=params.campaign_id,
        fallback_status=None,
    )
    _log_success("campaign.update", params.advertiser_id, result, payload)
    return result


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@_log_blocked_write("campaign.status_update", CAMPAIGN_STATUS_UPDATE_PATH)
@require_writes_enabled("marketing")
async def update_campaign_status(
    alias: str,
    advertiser_id: str,
    campaign_ids: list[str],
    operation_status: OperationStatus,
) -> CampaignWriteResult | JsonObject:
    try:
        params = CampaignStatusUpdate.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "campaign_ids": campaign_ids,
                "operation_status": operation_status,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    payload = await _post_campaign_write(
        params.alias,
        CAMPAIGN_STATUS_UPDATE_PATH,
        _request_payload(params, exclude={"alias"}),
    )
    result = _campaign_result(
        payload,
        fallback_campaign_id=params.campaign_ids[0],
        fallback_status=params.operation_status,
    )
    _log_success("campaign.status_update", params.advertiser_id, result, payload)
    return result


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@_log_blocked_write("campaign.delete", CAMPAIGN_DELETE_PATH)
@require_writes_enabled("marketing")
async def delete_campaign(
    alias: str,
    advertiser_id: str,
    campaign_ids: list[str],
) -> CampaignWriteResult | JsonObject:
    try:
        params = DeleteCampaignRequest.model_validate(
            {"alias": alias, "advertiser_id": advertiser_id, "campaign_ids": campaign_ids}
        )
    except ValidationError as exc:
        return _validation_error(exc)

    payload = await _post_campaign_write(
        params.alias,
        CAMPAIGN_DELETE_PATH,
        _delete_payload(params),
    )
    result = _campaign_result(
        payload,
        fallback_campaign_id=params.campaign_ids[0],
        fallback_status="DELETE",
    )
    _log_success("campaign.delete", params.advertiser_id, result, payload)
    return result


async def _post_campaign_write(alias: str, path: str, body: JsonObject) -> JsonObject:
    async with await _build_business_client(alias) as client:
        payload = await client.request("POST", path, json=body)
    return _raw_payload(payload)


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
        account, tokens = deserialize_account_record(raw_record)
        return account, tokens
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
    except json.JSONDecodeError as exc:
        raise KeychainUnavailableError("Stored app credentials are not valid JSON.") from exc
    credentials = AppCredentials.model_validate(payload)
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")
    return credentials


def _request_payload(model: BaseModel, *, exclude: set[str]) -> JsonObject:
    return cast(JsonObject, model.model_dump(exclude=exclude, exclude_none=True, mode="json"))


def _delete_payload(params: DeleteCampaignRequest) -> JsonObject:
    payload = _request_payload(params, exclude={"alias"})
    payload["operation_status"] = "DELETE"
    return payload


def _campaign_result(
    payload: JsonObject,
    *,
    fallback_campaign_id: str | None,
    fallback_status: str | None,
) -> CampaignWriteResult:
    campaign_id = _first_string(payload, "campaign_id", "campaign_ids") or fallback_campaign_id
    return {
        "campaign_id": campaign_id,
        "modify_time": _first_string(payload, "modify_time"),
        "status": _first_string(payload, "status", "operation_status") or fallback_status,
    }


def _validation_error(exc: ValidationError) -> JsonObject:
    return {
        "error": "validation_error",
        "message": "Campaign write input validation failed.",
        "details": exc.errors(include_url=False),
    }


def _log_success(
    action: str,
    advertiser_id: str,
    result: CampaignWriteResult,
    payload: JsonObject,
) -> None:
    logger.info(
        "TikTok Marketing campaign write succeeded",
        extra={
            "action": action,
            "advertiser_id": advertiser_id,
            "campaign_id": result.get("campaign_id"),
            "request_id": _first_string(payload, "request_id"),
        },
    )


def _would_have_done(
    action: str,
    endpoint: str,
    args: Sequence[object],
    kwargs: dict[str, object],
) -> JsonObject:
    advertiser_id = _argument_value("advertiser_id", 1, args, kwargs)
    campaign_id = _campaign_id_from_call(args, kwargs)
    details: JsonObject = {"action": action, "endpoint": endpoint}
    if advertiser_id is not None:
        details["advertiser_id"] = advertiser_id
    if campaign_id is not None:
        details["campaign_id"] = campaign_id
    return details


def _campaign_id_from_call(args: Sequence[object], kwargs: dict[str, object]) -> str | None:
    direct_campaign_id = _argument_value("campaign_id", 2, args, kwargs)
    if direct_campaign_id is not None:
        return direct_campaign_id

    campaign_ids = kwargs.get("campaign_ids")
    if campaign_ids is None and len(args) > 2:
        campaign_ids = args[2]
    return _string_from_value(campaign_ids)


def _argument_value(
    name: str,
    position: int,
    args: Sequence[object],
    kwargs: dict[str, object],
) -> str | None:
    if name in kwargs:
        return _string_from_value(kwargs[name])
    if len(args) > position:
        return _string_from_value(args[position])
    return None


def _first_string(payload: JsonObject, *keys: str) -> str | None:
    for key in keys:
        value = _string_from_value(payload.get(key))
        if value is not None:
            return value
    return None


def _string_from_value(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str) and first:
            return first
    return None


def _raw_payload(payload: object) -> JsonObject:
    if isinstance(payload, dict):
        return cast(JsonObject, payload)
    return {}


__all__ = [
    "CAMPAIGN_CREATE_PATH",
    "CAMPAIGN_DELETE_PATH",
    "CAMPAIGN_STATUS_UPDATE_PATH",
    "CAMPAIGN_UPDATE_PATH",
    "CampaignStatusUpdate",
    "CreateCampaignRequest",
    "DeleteCampaignRequest",
    "UpdateCampaignRequest",
    "create_campaign",
    "delete_campaign",
    "update_campaign",
    "update_campaign_status",
]
