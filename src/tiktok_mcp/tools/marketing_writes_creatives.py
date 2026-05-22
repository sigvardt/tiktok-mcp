# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyright: reportMissingImports=false
from __future__ import annotations

import json
import mimetypes
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, ClassVar, Literal, cast

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
from tiktok_mcp.envelopes import decode_business_response
from tiktok_mcp.marketing.asset_chunker import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNK_SIZE,
    chunk_file,
    sha256_file,
)
from tiktok_mcp.observability.rate_limit_tracker import record_request
from tiktok_mcp.server import app
from tiktok_mcp.types.accounts import Account, AccountTokens, ApiType
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import (
    AccountNotFoundError,
    AppCredentialsNotSetError,
    KeychainUnavailableError,
)

VIDEO_UPLOAD_PATH = "/open_api/v1.3/file/video/ad/upload/"
IMAGE_UPLOAD_PATH = "/open_api/v1.3/file/image/ad/upload/"
VIDEO_DELETE_PATH = "/open_api/v1.3/file/video/ad/delete/"
IMAGE_DELETE_PATH = "/open_api/v1.3/file/image/ad/delete/"

JsonObject = dict[str, object]
MultipartFile = tuple[str, bytes | BinaryIO, str]


class CreativeUploadParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    source_file_path: Path

    @field_validator("source_file_path")
    @classmethod
    def validate_source_file_path(cls, value: Path) -> Path:
        if not value.is_file():
            raise ValueError(f"source_file_path must point to an existing file: {value}")
        return value


class VideoUploadParams(CreativeUploadParams):
    chunk_size: int = Field(default=DEFAULT_CHUNK_SIZE, ge=DEFAULT_CHUNK_SIZE, le=MAX_CHUNK_SIZE)


class DeleteVideoParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    video_ids: list[str] = Field(min_length=1)


class DeleteImageParams(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    advertiser_id: str = Field(min_length=1)
    image_ids: list[str] = Field(min_length=1)


class VideoAssetUploadResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    video_id: str
    video_signature: str
    size: int | None = None
    format: str | None = None
    height: int | None = None
    width: int | None = None
    bit_rate: int | None = None
    duration: int | float | None = None
    file_name: str | None = None


class ImageAssetUploadResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    image_id: str
    image_url: str | None = None
    signature: str
    size: int | None = None
    format: str | None = None
    height: int | None = None
    width: int | None = None


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def upload_video_asset(
    alias: str,
    advertiser_id: str,
    source_file_path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> JsonObject:
    params = VideoUploadParams.model_validate(
        {
            "alias": alias,
            "advertiser_id": advertiser_id,
            "source_file_path": source_file_path,
            "chunk_size": chunk_size,
        }
    )
    signature = sha256_file(params.source_file_path)
    size = params.source_file_path.stat().st_size
    chunk_count = max(1, (size + params.chunk_size - 1) // params.chunk_size)

    async with await _build_business_client(params.alias) as client:
        chunks = chunk_file(params.source_file_path, params.chunk_size)
        for chunk_index, chunk in enumerate(chunks, start=1):
            payload = await _post_multipart(
                client,
                VIDEO_UPLOAD_PATH,
                data={
                    "advertiser_id": params.advertiser_id,
                    "video_signature": signature,
                    "file_name": params.source_file_path.name,
                    "chunk_index": str(chunk_index),
                    "chunk_count": str(chunk_count),
                    "chunk_size": str(len(chunk)),
                    "total_size": str(size),
                },
                files={
                    "video_file": (
                        params.source_file_path.name,
                        chunk,
                        _content_type(params.source_file_path, "video/mp4"),
                    )
                },
            )
            normalized = _normalize_video_upload_payload(
                payload,
                signature=signature,
                file_name=params.source_file_path.name,
                size=size,
            )
            if normalized is not None:
                return normalized.model_dump(mode="json")

    raise ValueError("TikTok video upload response did not include a video_id")


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def upload_image_asset(
    alias: str,
    advertiser_id: str,
    source_file_path: str,
) -> JsonObject:
    params = CreativeUploadParams.model_validate(
        {"alias": alias, "advertiser_id": advertiser_id, "source_file_path": source_file_path}
    )
    signature = sha256_file(params.source_file_path)
    size = params.source_file_path.stat().st_size

    async with await _build_business_client(params.alias) as client:
        with params.source_file_path.open("rb") as image_file:
            payload = await _post_multipart(
                client,
                IMAGE_UPLOAD_PATH,
                data={"advertiser_id": params.advertiser_id, "image_signature": signature},
                files={
                    "image_file": (
                        params.source_file_path.name,
                        image_file,
                        _content_type(params.source_file_path, "image/jpeg"),
                    )
                },
            )

    result = _normalize_image_upload_payload(payload, signature=signature, size=size)
    return result.model_dump(mode="json")


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def delete_video_asset(
    alias: str,
    advertiser_id: str,
    video_ids: list[str],
) -> JsonObject:
    params = DeleteVideoParams.model_validate(
        {"alias": alias, "advertiser_id": advertiser_id, "video_ids": video_ids}
    )
    async with await _build_business_client(params.alias) as client:
        payload = cast(
            JsonObject,
            await client.post(
                VIDEO_DELETE_PATH,
                json={"advertiser_id": params.advertiser_id, "video_ids": params.video_ids},
            ),
        )
    return _delete_result(payload, "video_ids", params.video_ids)


@app.tool(annotations=ToolAnnotations(destructiveHint=True))
@require_writes_enabled("marketing")
async def delete_image_asset(
    alias: str,
    advertiser_id: str,
    image_ids: list[str],
) -> JsonObject:
    params = DeleteImageParams.model_validate(
        {"alias": alias, "advertiser_id": advertiser_id, "image_ids": image_ids}
    )
    async with await _build_business_client(params.alias) as client:
        payload = cast(
            JsonObject,
            await client.post(
                IMAGE_DELETE_PATH,
                json={"advertiser_id": params.advertiser_id, "image_ids": params.image_ids},
            ),
        )
    return _delete_result(payload, "image_ids", params.image_ids)


async def _post_multipart(
    client: BusinessAPIClient,
    path: str,
    *,
    data: Mapping[str, str],
    files: Mapping[str, MultipartFile],
) -> JsonObject:
    tokens = await client._ensure_tokens()
    http_client = await client._http_client()
    response = await http_client.post(
        path,
        data=data,
        files=files,
        headers=client._auth_headers(tokens),
    )
    decoded = decode_business_response(response)
    await record_request(client.account.api_type, client.account.alias)
    return _json_object(decoded)


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
    raw_credentials = await backend.get(app_creds_key(ApiType.MARKETING, account.sandbox))
    if raw_credentials is None:
        raise AppCredentialsNotSetError(ApiType.MARKETING.value, account.sandbox)
    try:
        payload = cast(object, json.loads(raw_credentials))
        credentials = AppCredentials.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise KeychainUnavailableError(
            "Stored Marketing app credentials are invalid.",
            context={"api_type": ApiType.MARKETING.value, "sandbox": account.sandbox},
        ) from exc
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")
    return credentials


def _normalize_video_upload_payload(
    payload: Mapping[str, object],
    *,
    signature: str,
    file_name: str,
    size: int,
) -> VideoAssetUploadResult | None:
    candidate = _first_nested_object(payload, "video", "video_info", "creative")
    video_id = _optional_string(candidate, "video_id", "existing_video_id")
    if video_id is None:
        return None

    return VideoAssetUploadResult.model_validate(
        {
            "video_id": video_id,
            "video_signature": (
                _optional_string(candidate, "video_signature", "signature") or signature
            ),
            "size": _optional_int(candidate, "size", "file_size") or size,
            "format": _optional_string(candidate, "format", "video_format"),
            "height": _optional_int(candidate, "height"),
            "width": _optional_int(candidate, "width"),
            "bit_rate": _optional_int(candidate, "bit_rate", "bitrate"),
            "duration": _optional_number(candidate, "duration"),
            "file_name": _optional_string(candidate, "file_name") or file_name,
        }
    )


def _normalize_image_upload_payload(
    payload: Mapping[str, object],
    *,
    signature: str,
    size: int,
) -> ImageAssetUploadResult:
    candidate = _first_nested_object(payload, "image", "image_info", "creative")
    image_id = _optional_string(candidate, "image_id", "existing_image_id")
    if image_id is None:
        raise ValueError("TikTok image upload response did not include an image_id")

    return ImageAssetUploadResult.model_validate(
        {
            "image_id": image_id,
            "image_url": _optional_string(candidate, "image_url", "url"),
            "signature": _optional_string(candidate, "signature", "image_signature") or signature,
            "size": _optional_int(candidate, "size", "file_size") or size,
            "format": _optional_string(candidate, "format", "image_format"),
            "height": _optional_int(candidate, "height"),
            "width": _optional_int(candidate, "width"),
        }
    )


def _delete_result(
    payload: Mapping[str, object],
    id_key: Literal["video_ids", "image_ids"],
    ids: list[str],
) -> JsonObject:
    deleted_ids = payload.get(id_key)
    return {
        "deleted": True,
        id_key: deleted_ids if isinstance(deleted_ids, list) else ids,
        "raw_response": dict(payload),
    }


def _first_nested_object(payload: Mapping[str, object], *field_names: str) -> Mapping[str, object]:
    for field_name in field_names:
        value = payload.get(field_name)
        if isinstance(value, dict):
            return cast(Mapping[str, object], value)
    return payload


def _json_object(value: object) -> JsonObject:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    raise ValueError("TikTok response data must be a JSON object")


def _optional_string(payload: Mapping[str, object], *field_names: str) -> str | None:
    for field_name in field_names:
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def _optional_int(payload: Mapping[str, object], *field_names: str) -> int | None:
    for field_name in field_names:
        value = payload.get(field_name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _optional_number(payload: Mapping[str, object], *field_names: str) -> int | float | None:
    for field_name in field_names:
        value = payload.get(field_name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return value
    return None


def _content_type(path: Path, default: str) -> str:
    guessed_type, _encoding = mimetypes.guess_type(path.name)
    return guessed_type or default


__all__ = [
    "IMAGE_DELETE_PATH",
    "IMAGE_UPLOAD_PATH",
    "VIDEO_DELETE_PATH",
    "VIDEO_UPLOAD_PATH",
    "delete_image_asset",
    "delete_video_asset",
    "upload_image_asset",
    "upload_video_asset",
]
