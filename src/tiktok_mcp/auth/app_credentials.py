"""Helpers for parsing stored TikTok app credentials."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError

from tiktok_mcp.auth.redactor import register_token
from tiktok_mcp.types.app_credentials import AppCredentials
from tiktok_mcp.types.errors import StoredCredentialError

CLIENT_SECRET_FIELD = "client_secret"


@dataclass(frozen=True)
class StoredAppCredentials:
    credentials: AppCredentials
    redirect_uri: str | None


def deserialize_stored_app_credentials(blob: str) -> StoredAppCredentials:
    try:
        payload = cast(object, json.loads(blob))
    except json.JSONDecodeError as exc:
        raise StoredCredentialError(
            "Stored app credentials are not valid JSON.",
            context={"record_type": "app_credentials"},
        ) from exc

    if not isinstance(payload, dict):
        raise StoredCredentialError(
            "Stored app credentials must be a JSON object.",
            context={"record_type": "app_credentials"},
        )

    credentials_payload, redirect_uri = split_app_credentials_payload(
        {str(key): value for key, value in payload.items()}
    )
    try:
        credentials = AppCredentials.model_validate(credentials_payload)
    except ValidationError as exc:
        raise StoredCredentialError(
            "Stored app credentials failed schema validation.",
            context={
                "record_type": "app_credentials",
                "details": exc.errors(include_url=False, include_input=False),
            },
        ) from exc
    register_token(credentials.client_id.get_secret_value(), "client_id")
    register_token(credentials.client_secret.get_secret_value(), "client_secret")
    return StoredAppCredentials(credentials=credentials, redirect_uri=redirect_uri)


def split_app_credentials_payload(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], str | None]:
    nested_credentials = payload.get("credentials")
    if isinstance(nested_credentials, dict):
        credentials_payload = {str(key): value for key, value in nested_credentials.items()}
        redirect_uri = _optional_string_value(payload, "redirect_uri")
        if redirect_uri is None:
            redirect_uri = _optional_string_value(credentials_payload, "redirect_uri")
    else:
        credentials_payload = dict(payload)
        redirect_uri = _optional_string_value(payload, "redirect_uri")

    filtered_credentials = {
        key: credentials_payload[key]
        for key in {"api_type", "sandbox", "client_id", CLIENT_SECRET_FIELD, "created_at"}
        if key in credentials_payload
    }
    return filtered_credentials, redirect_uri


def _optional_string_value(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None
