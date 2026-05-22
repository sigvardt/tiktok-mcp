from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportUnknownArgumentType=false
import json
from collections.abc import Mapping
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

AD_CREATE_PATH = "/open_api/v1.3/ad/create/"
AD_UPDATE_PATH = "/open_api/v1.3/ad/update/"
AD_STATUS_UPDATE_PATH = "/open_api/v1.3/ad/status/update/"
AD_DELETE_PATH = "/open_api/v1.3/ad/delete/"

AdFormat = Literal["SINGLE_VIDEO", "COLLECTION_ADS", "CATALOG_CAROUSEL"]
CreativeMaterialMode = Literal["CUSTOM"]
AdOperationStatus = Literal["ENABLE", "DISABLE"]
JsonObject = dict[str, object]


class CreateAdRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    adgroup_id: str = Field(min_length=1)
    creative_material_mode: CreativeMaterialMode
    ad_name: str = Field(min_length=1)
    ad_format: AdFormat
    identity_type: str = Field(min_length=1)
    identity_id: str = Field(min_length=1)
    video_id: str | None = Field(default=None, min_length=1)
    image_ids: list[str] | None = Field(default=None, min_length=1)
    ad_text: str = Field(min_length=1)
    landing_page_url: str | None = Field(default=None, min_length=1)
    call_to_action: str | None = Field(default=None, min_length=1)
    display_name: str | None = Field(default=None, min_length=1)
    creative_authorized: bool = False
    spark_ads_post_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_creative_assets(self) -> Self:
        if self.creative_authorized and self.spark_ads_post_id is None:
            raise ValueError("creative_authorized=true requires spark_ads_post_id")
        if (self.video_id is None) == (self.image_ids is None):
            raise ValueError("exactly one of video_id or image_ids is required")
        return self

    def to_payload(self) -> JsonObject:
        return _compact_payload(
            {
                "advertiser_id": self.advertiser_id,
                "adgroup_id": self.adgroup_id,
                "creative_material_mode": self.creative_material_mode,
                "ad_name": self.ad_name,
                "ad_format": self.ad_format,
                "identity_type": self.identity_type,
                "identity_id": self.identity_id,
                "video_id": self.video_id,
                "image_ids": self.image_ids,
                "ad_text": self.ad_text,
                "landing_page_url": self.landing_page_url,
                "call_to_action": self.call_to_action,
                "display_name": self.display_name,
                "creative_authorized": self.creative_authorized,
                "spark_ads_post_id": self.spark_ads_post_id,
            }
        )


class UpdateAdRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    ad_id: str = Field(min_length=1)
    creative_material_mode: CreativeMaterialMode | None = None
    ad_name: str | None = Field(default=None, min_length=1)
    ad_format: AdFormat | None = None
    identity_type: str | None = Field(default=None, min_length=1)
    identity_id: str | None = Field(default=None, min_length=1)
    video_id: str | None = Field(default=None, min_length=1)
    image_ids: list[str] | None = Field(default=None, min_length=1)
    ad_text: str | None = Field(default=None, min_length=1)
    landing_page_url: str | None = Field(default=None, min_length=1)
    call_to_action: str | None = Field(default=None, min_length=1)
    display_name: str | None = Field(default=None, min_length=1)
    creative_authorized: bool = False
    spark_ads_post_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_creative_assets(self) -> Self:
        if self.creative_authorized and self.spark_ads_post_id is None:
            raise ValueError("creative_authorized=true requires spark_ads_post_id")
        if self.video_id is not None and self.image_ids is not None:
            raise ValueError("video_id and image_ids cannot both be supplied")
        return self

    def to_payload(self) -> JsonObject:
        return _compact_payload(
            {
                "advertiser_id": self.advertiser_id,
                "ad_id": self.ad_id,
                "creative_material_mode": self.creative_material_mode,
                "ad_name": self.ad_name,
                "ad_format": self.ad_format,
                "identity_type": self.identity_type,
                "identity_id": self.identity_id,
                "video_id": self.video_id,
                "image_ids": self.image_ids,
                "ad_text": self.ad_text,
                "landing_page_url": self.landing_page_url,
                "call_to_action": self.call_to_action,
                "display_name": self.display_name,
                "creative_authorized": self.creative_authorized,
                "spark_ads_post_id": self.spark_ads_post_id,
            }
        )


class UpdateAdStatusRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    ad_id: str = Field(min_length=1)
    operation_status: AdOperationStatus

    def to_payload(self) -> JsonObject:
        return {
            "advertiser_id": self.advertiser_id,
            "ad_ids": [self.ad_id],
            "operation_status": self.operation_status,
        }


class DeleteAdRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    ad_id: str = Field(min_length=1)

    def to_payload(self) -> JsonObject:
        return {"advertiser_id": self.advertiser_id, "ad_ids": [self.ad_id]}


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def create_ad(
    alias: str,
    advertiser_id: str,
    adgroup_id: str,
    creative_material_mode: CreativeMaterialMode,
    ad_name: str,
    ad_format: AdFormat,
    identity_type: str,
    identity_id: str,
    ad_text: str,
    video_id: str | None = None,
    image_ids: list[str] | None = None,
    landing_page_url: str | None = None,
    call_to_action: str | None = None,
    display_name: str | None = None,
    creative_authorized: bool = False,
    spark_ads_post_id: str | None = None,
) -> JsonObject:
    try:
        request = CreateAdRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "adgroup_id": adgroup_id,
                "creative_material_mode": creative_material_mode,
                "ad_name": ad_name,
                "ad_format": ad_format,
                "identity_type": identity_type,
                "identity_id": identity_id,
                "video_id": video_id,
                "image_ids": image_ids,
                "ad_text": ad_text,
                "landing_page_url": landing_page_url,
                "call_to_action": call_to_action,
                "display_name": display_name,
                "creative_authorized": creative_authorized,
                "spark_ads_post_id": spark_ads_post_id,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_json(request.alias, AD_CREATE_PATH, request.to_payload())


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def update_ad(
    alias: str,
    advertiser_id: str,
    ad_id: str,
    creative_material_mode: CreativeMaterialMode | None = None,
    ad_name: str | None = None,
    ad_format: AdFormat | None = None,
    identity_type: str | None = None,
    identity_id: str | None = None,
    video_id: str | None = None,
    image_ids: list[str] | None = None,
    ad_text: str | None = None,
    landing_page_url: str | None = None,
    call_to_action: str | None = None,
    display_name: str | None = None,
    creative_authorized: bool = False,
    spark_ads_post_id: str | None = None,
) -> JsonObject:
    try:
        request = UpdateAdRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "ad_id": ad_id,
                "creative_material_mode": creative_material_mode,
                "ad_name": ad_name,
                "ad_format": ad_format,
                "identity_type": identity_type,
                "identity_id": identity_id,
                "video_id": video_id,
                "image_ids": image_ids,
                "ad_text": ad_text,
                "landing_page_url": landing_page_url,
                "call_to_action": call_to_action,
                "display_name": display_name,
                "creative_authorized": creative_authorized,
                "spark_ads_post_id": spark_ads_post_id,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_json(request.alias, AD_UPDATE_PATH, request.to_payload())


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def update_ad_status(
    alias: str,
    advertiser_id: str,
    ad_id: str,
    operation_status: AdOperationStatus,
) -> JsonObject:
    try:
        request = UpdateAdStatusRequest.model_validate(
            {
                "alias": alias,
                "advertiser_id": advertiser_id,
                "ad_id": ad_id,
                "operation_status": operation_status,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_json(request.alias, AD_STATUS_UPDATE_PATH, request.to_payload())


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def delete_ad(alias: str, advertiser_id: str, ad_id: str) -> JsonObject:
    try:
        request = DeleteAdRequest.model_validate(
            {"alias": alias, "advertiser_id": advertiser_id, "ad_id": ad_id}
        )
    except ValidationError as exc:
        return _validation_error(exc)
    return await _post_json(request.alias, AD_DELETE_PATH, request.to_payload())


async def _post_json(alias: str, path: str, json_payload: JsonObject) -> JsonObject:
    async with await _build_business_client(alias) as client:
        payload = await client.post(path, json=json_payload)
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
        raise KeychainUnavailableError("Stored Marketing app credentials are invalid.") from exc
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")
    return credentials


def _validation_error(exc: ValidationError) -> JsonObject:
    details: list[JsonObject] = []
    messages: list[str] = []
    for error in exc.errors(include_url=False):
        message = str(error.get("msg", "validation error"))
        messages.append(message)
        details.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "message": message,
                "type": str(error.get("type", "value_error")),
            }
        )

    return {
        "error": "validation_error",
        "message": "; ".join(messages) or "validation error",
        "context": {"details": details},
    }


def _compact_payload(payload: Mapping[str, object | None]) -> JsonObject:
    return {key: value for key, value in payload.items() if value is not None}


def _raw_payload(payload: object) -> JsonObject:
    if isinstance(payload, dict):
        return dict(cast(Mapping[str, object], payload))
    return cast(JsonObject, payload)


__all__ = [
    "AD_CREATE_PATH",
    "AD_DELETE_PATH",
    "AD_STATUS_UPDATE_PATH",
    "AD_UPDATE_PATH",
    "CreateAdRequest",
    "DeleteAdRequest",
    "UpdateAdRequest",
    "UpdateAdStatusRequest",
    "create_ad",
    "delete_ad",
    "update_ad",
    "update_ad_status",
]
