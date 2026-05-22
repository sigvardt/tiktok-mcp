from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportMissingImports=false
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false, reportExplicitAny=false
import json
import logging
from typing import Any, BinaryIO, cast

from httpx._types import RequestFiles
from mcp.types import ToolAnnotations

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
from tiktok_mcp.marketing.audience_hashing import (
    HashedAudienceCSVStream,
    estimate_csv_row_count,
    filename_hash,
    validate_audience_source_path,
)
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountNotFoundError,
    AppCredentialsNotSetError,
    KeychainUnavailableError,
)

CREATE_CUSTOM_AUDIENCE_PATH = "/open_api/v1.3/dmp/custom_audience/create/"
UPDATE_CUSTOM_AUDIENCE_PATH = "/open_api/v1.3/dmp/custom_audience/update/"
DELETE_CUSTOM_AUDIENCE_PATH = "/open_api/v1.3/dmp/custom_audience/delete/"

logger = logging.getLogger(__name__)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def create_custom_audience(
    alias: str,
    advertiser_id: str,
    audience_name: str,
    source_file_path: str,
    match_keys: list[str],
) -> dict[str, Any]:
    validated_path = validate_audience_source_path(source_file_path)
    if isinstance(validated_path, dict):
        return validated_path

    row_count_estimate = estimate_csv_row_count(validated_path)
    file_size_bytes = validated_path.stat().st_size
    logger.info(
        "Preparing Custom Audience upload",
        extra={
            "filename_hash": filename_hash(validated_path),
            "row_count_estimate": row_count_estimate,
            "file_size_bytes": file_size_bytes,
        },
    )

    upload_stream = HashedAudienceCSVStream(validated_path, match_keys)
    data = {
        "advertiser_id": advertiser_id,
        "custom_audience_name": audience_name,
        "match_keys": json.dumps(match_keys, separators=(",", ":")),
    }
    # `HashedAudienceCSVStream` is structurally a `BinaryIO` (`read`/`seek`/`tell`),
    # but basedpyright cannot infer that duck-typed contract here.
    files: RequestFiles = {
        "file": (
            "audience.csv",
            cast(BinaryIO, upload_stream),  # pyright: ignore[reportInvalidCast]  # noqa
            "text/csv",
        )
    }
    async with await _build_business_client(alias) as client:
        payload = await client.post(CREATE_CUSTOM_AUDIENCE_PATH, data=data, files=files)
    return _raw_payload(payload)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def update_custom_audience_name(
    alias: str,
    advertiser_id: str,
    custom_audience_id: str,
    audience_name: str,
) -> dict[str, Any]:
    payload = {
        "advertiser_id": advertiser_id,
        "custom_audience_id": custom_audience_id,
        "custom_audience_name": audience_name,
    }
    async with await _build_business_client(alias) as client:
        response = await client.post(UPDATE_CUSTOM_AUDIENCE_PATH, json=payload)
    return _raw_payload(response)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def delete_custom_audience(
    alias: str,
    advertiser_id: str,
    custom_audience_id: str,
) -> dict[str, Any]:
    payload = {"advertiser_id": advertiser_id, "custom_audience_id": custom_audience_id}
    async with await _build_business_client(alias) as client:
        response = await client.post(DELETE_CUSTOM_AUDIENCE_PATH, json=payload)
    return _raw_payload(response)


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
    except json.JSONDecodeError as exc:
        raise KeychainUnavailableError("Stored app credentials are not valid JSON.") from exc
    credentials = AppCredentials.model_validate(payload)
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")
    return credentials


def _raw_payload(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    return cast(dict[str, Any], payload)


__all__ = [
    "CREATE_CUSTOM_AUDIENCE_PATH",
    "DELETE_CUSTOM_AUDIENCE_PATH",
    "UPDATE_CUSTOM_AUDIENCE_PATH",
    "create_custom_audience",
    "delete_custom_audience",
    "update_custom_audience_name",
]
