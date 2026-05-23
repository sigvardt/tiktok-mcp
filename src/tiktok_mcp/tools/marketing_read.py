from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportExplicitAny=false, reportUnknownArgumentType=false
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar, cast

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError

from tiktok_mcp.api.business import BusinessAPIClient
from tiktok_mcp.api.marketing import (
    Ad,
    AdGroup,
    Advertiser,
    AdvertiserInfo,
    BusinessCenter,
    Campaign,
)
from tiktok_mcp.auth.http_sanitizer import SanitizedHttpxError
from tiktok_mcp.auth.keychain import (
    KeychainBackend,
    app_creds_key,
    deserialize_account_record,
    get_backend,
)
from tiktok_mcp.decorators import mark_read_only
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountNotFoundError,
    AppCredentialsNotSetError,
    KeychainUnavailableError,
)

AD_GET_PATH = "/open_api/v1.3/ad/get/"
ADGROUP_GET_PATH = "/open_api/v1.3/adgroup/get/"
ADVERTISER_INFO_PATH = "/open_api/v1.3/advertiser/info/"
BC_ADVERTISER_GET_PATH = "/open_api/v1.3/bc/asset/get/"
BC_GET_PATH = "/open_api/v1.3/bc/get/"
CAMPAIGN_GET_PATH = "/open_api/v1.3/campaign/get/"
USER_INFO_PATH = "/open_api/v1.3/user/info/"
BC_ENDPOINTS_NOT_AVAILABLE_IN_SANDBOX_MESSAGE = (
    "Business Center endpoints are not available in the TikTok Business sandbox. "
    "Use a production account with TIKTOK_MCP_LIVE_ACCOUNT_SAFETY configured to enable."
)

ACCOUNT_KEY_RE = re.compile(
    r"^tiktok-mcp::marketing::(?P<mode>sandbox|production)::"
    + r"account::(?P<alias>[a-z0-9-]{3,50})$"
)

QueryParams = Mapping[str, str | int | float | bool | None]
ModelT = TypeVar("ModelT", bound=BaseModel)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_list_advertisers(alias: str) -> dict[str, Any]:
    """Return OAuth-time advertiser discovery or a documented access-token limitation."""
    backend = await get_backend()
    account, tokens = await _load_marketing_account(backend, alias)
    app_credentials = await _load_app_credentials(backend, account)
    advertiser_id = _stored_advertiser_id(account)
    async with _build_business_client(account, app_credentials, tokens, backend) as client:
        if advertiser_id is not None:
            params = {"advertiser_ids": _json_array([advertiser_id])}
            payload = await client.request("GET", ADVERTISER_INFO_PATH, params=params)
            return {
                "advertisers": _list_from_payload(
                    payload,
                    "list",
                    "advertisers",
                    "advertiser_info",
                )
            }
        user_payload = await client.request("GET", USER_INFO_PATH)
    return _advertiser_discovery_not_supported(user_payload)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_get_advertiser_info(
    alias: str,
    advertiser_id: str,
    fields: Sequence[str] | None = None,
) -> AdvertiserInfo:
    params = _compact_params(
        {
            "advertiser_ids": _json_array([advertiser_id]),
            "fields": _json_array(fields),
        }
    )
    async with await _marketing_client(alias) as client:
        payload = await client.request("GET", ADVERTISER_INFO_PATH, params=params)
    return _model_from_payload(payload, AdvertiserInfo, "list", "advertisers", "advertiser_info")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_list_business_centers(alias: str) -> list[BusinessCenter] | dict[str, object]:
    """List Business Centers, or report the TikTok sandbox BC endpoint limitation."""
    async with await _marketing_client(alias) as client:
        try:
            payload = await client.request("GET", BC_GET_PATH)
        except SanitizedHttpxError as exc:
            if _is_sandbox_endpoint_404(client.account, exc, BC_GET_PATH):
                return _sandbox_bc_endpoint_not_available(
                    tool="marketing_list_business_centers",
                    alias=alias,
                    endpoint=BC_GET_PATH,
                    alternative_tools=(
                        "marketing_list_advertisers",
                        "marketing_list_bc_advertisers",
                    ),
                )
            raise
    return _models_from_payload(payload, BusinessCenter, "list", "business_centers", "bc_list")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_list_bc_advertisers(
    alias: str,
    bc_id: str,
) -> list[Advertiser] | dict[str, object]:
    """List BC advertiser assets, or report the TikTok sandbox BC endpoint limitation."""
    params = {"bc_id": bc_id, "asset_type": "ADVERTISER"}
    async with await _marketing_client(alias) as client:
        try:
            payload = await client.request("GET", BC_ADVERTISER_GET_PATH, params=params)
        except SanitizedHttpxError as exc:
            if _is_sandbox_endpoint_404(client.account, exc, BC_ADVERTISER_GET_PATH):
                return _sandbox_bc_endpoint_not_available(
                    tool="marketing_list_bc_advertisers",
                    alias=alias,
                    endpoint=BC_ADVERTISER_GET_PATH,
                    alternative_tools=("marketing_list_advertisers",),
                )
            raise
    return _models_from_payload(payload, Advertiser, "list", "advertisers", "advertiser_list")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_list_campaigns(
    alias: str,
    advertiser_id: str,
    filtering: Mapping[str, Any] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    return await _paged_get(alias, CAMPAIGN_GET_PATH, advertiser_id, filtering, page, page_size)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_list_adgroups(
    alias: str,
    advertiser_id: str,
    filtering: Mapping[str, Any] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    return await _paged_get(alias, ADGROUP_GET_PATH, advertiser_id, filtering, page, page_size)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_list_ads(
    alias: str,
    advertiser_id: str,
    filtering: Mapping[str, Any] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    return await _paged_get(alias, AD_GET_PATH, advertiser_id, filtering, page, page_size)


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_get_campaign(
    alias: str,
    advertiser_id: str,
    campaign_id: str,
    fields: Sequence[str] | None = None,
) -> Campaign:
    payload = await _get_entity(
        alias,
        CAMPAIGN_GET_PATH,
        advertiser_id,
        "campaign_ids",
        campaign_id,
        fields,
    )
    return _model_from_payload(payload, Campaign, "list", "campaigns", "campaign_list")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_get_adgroup(
    alias: str,
    advertiser_id: str,
    adgroup_id: str,
    fields: Sequence[str],
) -> AdGroup:
    payload = await _get_entity(
        alias,
        ADGROUP_GET_PATH,
        advertiser_id,
        "adgroup_ids",
        adgroup_id,
        fields,
    )
    return _model_from_payload(payload, AdGroup, "list", "adgroups", "adgroup_list")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
@mark_read_only
async def marketing_get_ad(
    alias: str,
    advertiser_id: str,
    ad_id: str,
    fields: Sequence[str],
) -> Ad:
    payload = await _get_entity(alias, AD_GET_PATH, advertiser_id, "ad_ids", ad_id, fields)
    return _model_from_payload(payload, Ad, "list", "ads", "ad_list")


async def _paged_get(
    alias: str,
    path: str,
    advertiser_id: str,
    filtering: Mapping[str, Any] | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    params = _compact_params(
        {
            "advertiser_id": advertiser_id,
            "filtering": _json_object(filtering),
            "page": page,
            "page_size": page_size,
        }
    )
    async with await _marketing_client(alias) as client:
        payload = await client.request("GET", path, params=params)
    return _raw_payload(payload)


async def _get_entity(
    alias: str,
    path: str,
    advertiser_id: str,
    id_filter_name: str,
    entity_id: str,
    fields: Sequence[str] | None,
) -> dict[str, Any]:
    filtering = {id_filter_name: [entity_id]}
    params = _compact_params(
        {
            "advertiser_id": advertiser_id,
            "filtering": _json_object(filtering),
            "fields": _json_array(fields),
            "page": 1,
            "page_size": 1,
        }
    )
    async with await _marketing_client(alias) as client:
        payload = await client.request("GET", path, params=params)
    return _raw_payload(payload)


async def _marketing_client(alias: str) -> BusinessAPIClient:
    backend = await get_backend()
    account, tokens = await _load_marketing_account(backend, alias)
    app_credentials = await _load_app_credentials(backend, account)
    return _build_business_client(account, app_credentials, tokens, backend)


def _build_business_client(
    account: Account,
    app_credentials: AppCredentials,
    tokens: AccountTokens,
    backend: KeychainBackend,
) -> BusinessAPIClient:
    return BusinessAPIClient(account, app_credentials, tokens=tokens, backend=backend)


async def _load_marketing_account(
    backend: KeychainBackend,
    alias: str,
) -> tuple[Account, AccountTokens]:
    for key in await backend.list_keys("tiktok-mcp::marketing::"):
        match = ACCOUNT_KEY_RE.fullmatch(key)
        if match is None or match.group("alias") != alias:
            continue
        raw_record = await backend.get(key)
        if raw_record is None:
            continue
        return deserialize_account_record(raw_record)
    raise AccountNotFoundError(alias, api_type=ApiType.MARKETING.value)


async def _load_app_credentials(
    backend: KeychainBackend,
    account: Account,
) -> AppCredentials:
    raw_credentials = await backend.get(app_creds_key(ApiType.MARKETING, account.sandbox))
    if raw_credentials is None:
        raise AppCredentialsNotSetError(ApiType.MARKETING.value, account.sandbox)
    try:
        payload = cast(object, json.loads(raw_credentials))
        return AppCredentials.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise KeychainUnavailableError(
            "Stored Marketing app credentials are invalid.",
            context={"api_type": ApiType.MARKETING.value, "sandbox": account.sandbox},
        ) from exc


def _models_from_payload(
    payload: object,
    model: type[ModelT],
    *candidate_keys: str,
) -> list[ModelT]:
    return [model.model_validate(item) for item in _list_from_payload(payload, *candidate_keys)]


def _stored_advertiser_id(account: Account) -> str | None:
    if account.tiktok_id.endswith("-unknown"):
        return None
    return account.tiktok_id


def _advertiser_discovery_not_supported(payload: object) -> dict[str, Any]:
    user_info = _raw_payload(payload)
    return {
        "endpoint_not_supported_for_this_token_type": True,
        "reason": (
            "TikTok does not expose an access-token-only endpoint for listing authorized "
            "Marketing advertisers at runtime. Store advertiser_ids from the OAuth token "
            "response, or call marketing_get_advertiser_info with a known advertiser_id."
        ),
        "advertisers": [],
        "user": {
            "core_user_id": user_info.get("core_user_id"),
            "display_name": user_info.get("display_name"),
        },
    }


def _is_sandbox_endpoint_404(
    account: Account,
    exc: SanitizedHttpxError,
    endpoint: str,
) -> bool:
    return account.sandbox and exc.status == 404 and exc.url_path == endpoint


def _sandbox_bc_endpoint_not_available(
    *,
    tool: str,
    alias: str,
    endpoint: str,
    alternative_tools: Sequence[str],
) -> dict[str, object]:
    return {
        "endpoint_not_available_in_sandbox": True,
        "tool": tool,
        "alias": alias,
        "endpoint": endpoint,
        "message": BC_ENDPOINTS_NOT_AVAILABLE_IN_SANDBOX_MESSAGE,
        "alternative_tools": list(alternative_tools),
    }


def _model_from_payload(payload: object, model: type[ModelT], *candidate_keys: str) -> ModelT:
    raw_payload = _raw_payload(payload)
    for key in candidate_keys:
        value = raw_payload.get(key)
        if isinstance(value, list) and value:
            return model.model_validate(value[0])
        if isinstance(value, dict):
            return model.model_validate(value)
    return model.model_validate(raw_payload)


def _list_from_payload(payload: object, *candidate_keys: str) -> list[object]:
    raw_payload = _raw_payload(payload)
    for key in candidate_keys:
        value = raw_payload.get(key)
        if isinstance(value, list):
            return list(value)
    data = raw_payload.get("data")
    if isinstance(data, list):
        return list(data)
    return []


def _raw_payload(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    return cast(dict[str, Any], payload)


def _compact_params(params: Mapping[str, str | int | float | bool | None]) -> QueryParams:
    return {key: value for key, value in params.items() if value is not None}


def _json_array(values: Sequence[str] | None) -> str | None:
    if values is None:
        return None
    return json.dumps(list(values), separators=(",", ":"))


def _json_object(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(dict(value), separators=(",", ":"), sort_keys=True)


__all__ = [
    "marketing_get_ad",
    "marketing_get_adgroup",
    "marketing_get_advertiser_info",
    "marketing_get_campaign",
    "marketing_list_ads",
    "marketing_list_adgroups",
    "marketing_list_advertisers",
    "marketing_list_bc_advertisers",
    "marketing_list_business_centers",
    "marketing_list_campaigns",
]
