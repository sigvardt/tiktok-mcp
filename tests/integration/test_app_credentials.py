from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import TracebackType

import httpx
import pytest

import tiktok_mcp.auth.keychain as keychain_module
from tiktok_mcp.api.business.urls import BUSINESS_ACCESS_TOKEN_PATH, business_url
from tiktok_mcp.auth.keychain import app_creds_key
from tiktok_mcp.tools import app_credentials as app_credentials_tools
from tiktok_mcp.tools.app_credentials import (
    list_app_credentials,
    set_app_credentials,
    verify_app_credentials,
)
from tiktok_mcp.types.accounts import ApiType

DISPLAY_VERIFY_URL = "https://open.tiktokapis.com/v2/oauth/token/"
BUSINESS_VERIFY_URL = business_url(BUSINESS_ACCESS_TOKEN_PATH, sandbox=False)
BUSINESS_SANDBOX_VERIFY_URL = business_url(BUSINESS_ACCESS_TOKEN_PATH, sandbox=True)
SECRET_MARKER = "hunter2_marker_secret_xyz"


class MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.values if key.startswith(prefix))


@pytest.fixture
def memory_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryBackend]:
    backend = MemoryBackend()
    monkeypatch.setattr(keychain_module, "_backend", backend)
    yield backend
    monkeypatch.setattr(keychain_module, "_backend", None)


@pytest.mark.asyncio
async def test_set_app_credentials_happy_path(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")

    response = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display_client_12345",
        client_secret=SECRET_MARKER,
        sandbox=True,
    )

    assert response["api_type"] == "display"
    assert response["sandbox"] is True
    assert response["client_secret_set"] is True
    assert response["client_id_fingerprint"] == "disp...len=20"
    assert SECRET_MARKER not in json.dumps(response, sort_keys=True)


@pytest.mark.asyncio
async def test_set_app_credentials_blocked_when_account_changes_disabled(
    memory_backend: MemoryBackend,
) -> None:
    _ = memory_backend

    response = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="blocked_client",
        client_secret=SECRET_MARKER,
    )

    assert response["error"] == "account_changes_disabled"
    assert response["tool"] == "set_app_credentials"


@pytest.mark.asyncio
async def test_list_app_credentials_returns_fingerprints_only(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    display_id = "display_client_abc"
    marketing_id = "marketing_client_xyz"

    _ = await set_app_credentials(
        ApiType.DISPLAY,
        client_id=display_id,
        client_secret="display_secret_xyz",
        sandbox=True,
    )
    _ = await set_app_credentials(
        ApiType.MARKETING,
        client_id=marketing_id,
        client_secret="marketing_secret_xyz",
    )

    response = await list_app_credentials()
    payload = json.dumps(response, sort_keys=True)

    assert response["count"] == 2
    assert len(response["credentials"]) == 2
    assert display_id not in payload
    assert marketing_id not in payload
    assert "display_secret_xyz" not in payload
    assert "marketing_secret_xyz" not in payload
    assert all("client_id" not in entry for entry in response["credentials"])


@pytest.mark.asyncio
async def test_verify_happy_path(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    _ = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display_client_verify",
        client_secret=SECRET_MARKER,
    )
    calls = _install_httpx_response(
        monkeypatch,
        _json_response({}, url=DISPLAY_VERIFY_URL),
    )

    response = await verify_app_credentials(ApiType.DISPLAY)

    assert response["valid"] is True
    assert response["error_code"] is None
    assert _is_recent_datetime(str(response["verified_at"]))
    assert len(calls) == 1
    assert calls[0][0] == DISPLAY_VERIFY_URL


@pytest.mark.asyncio
async def test_verify_invalid_client(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    _ = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display_client_invalid",
        client_secret=SECRET_MARKER,
    )
    _ = _install_httpx_response(
        monkeypatch,
        _json_response({"error": "invalid_client"}, url=DISPLAY_VERIFY_URL, status_code=401),
    )

    response = await verify_app_credentials(ApiType.DISPLAY)

    assert response["valid"] is False
    assert response["error_code"] == "invalid_client"


@pytest.mark.asyncio
async def test_verify_does_not_persist(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    _ = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display_client_ephemeral",
        client_secret=SECRET_MARKER,
    )
    before = await list_app_credentials()
    _ = _install_httpx_response(monkeypatch, _json_response({}, url=DISPLAY_VERIFY_URL))

    _ = await verify_app_credentials(ApiType.DISPLAY)
    after = await list_app_credentials()

    assert after == before
    assert all("last_verified_at" not in entry for entry in after["credentials"])
    assert all("verified" not in entry for entry in after["credentials"])


@pytest.mark.asyncio
async def test_verify_credentials_not_set_returns_not_found(
    memory_backend: MemoryBackend,
) -> None:
    _ = memory_backend

    response = await verify_app_credentials(ApiType.DISPLAY)

    assert response["valid"] is False
    assert response["error_code"] == "not_found"
    assert response["error_message"] == (
        "No app credentials registered for this api_type/sandbox combo."
    )


@pytest.mark.asyncio
async def test_secret_never_in_response(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    set_response = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display_client_secret_scan",
        client_secret=SECRET_MARKER,
    )
    list_response = await list_app_credentials()
    _ = _install_httpx_response(monkeypatch, _json_response({}, url=DISPLAY_VERIFY_URL))
    verify_response = await verify_app_credentials(ApiType.DISPLAY)

    assert SECRET_MARKER not in json.dumps(set_response, sort_keys=True)
    assert SECRET_MARKER not in json.dumps(list_response, sort_keys=True)
    assert SECRET_MARKER not in json.dumps(verify_response, sort_keys=True)


@pytest.mark.asyncio
async def test_sandbox_vs_production_isolated(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    sandbox_key = app_creds_key(ApiType.DISPLAY, sandbox=True)
    production_key = app_creds_key(ApiType.DISPLAY, sandbox=False)

    _ = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display_sandbox_client",
        client_secret="sandbox_secret_xyz",
        sandbox=True,
    )
    _ = await set_app_credentials(
        ApiType.DISPLAY,
        client_id="display_prod_client",
        client_secret="production_secret_xyz",
    )
    response = await list_app_credentials()
    fingerprints = {entry["client_id_fingerprint"] for entry in response["credentials"]}

    assert sandbox_key != production_key
    assert sandbox_key in memory_backend.values
    assert production_key in memory_backend.values
    assert memory_backend.values[sandbox_key] != memory_backend.values[production_key]
    assert response["count"] == 2
    assert fingerprints == {"disp...len=22", "disp...len=19"}
    assert "sandbox_secret_xyz" not in json.dumps(response, sort_keys=True)
    assert "production_secret_xyz" not in json.dumps(response, sort_keys=True)


@pytest.mark.asyncio
async def test_verify_marketing_invalid_auth_code_probe_is_valid(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    _ = await set_app_credentials(
        ApiType.MARKETING,
        client_id="marketing_client_probe",
        client_secret=SECRET_MARKER,
    )
    calls = _install_httpx_response(
        monkeypatch,
        _json_response(
            {"code": 40105, "message": "invalid auth_code"},
            url=BUSINESS_VERIFY_URL,
            status_code=400,
        ),
    )

    response = await verify_app_credentials(ApiType.MARKETING)

    assert response["valid"] is True
    assert response["error_code"] is None
    assert len(calls) == 1
    assert calls[0][0] == BUSINESS_VERIFY_URL
    assert calls[0][1]["auth_code"] == ""


@pytest.mark.asyncio
async def test_verify_marketing_sandbox_uses_sandbox_host(
    memory_backend: MemoryBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = memory_backend
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "1")
    _ = await set_app_credentials(
        ApiType.MARKETING,
        client_id="marketing_sandbox_probe",
        client_secret=SECRET_MARKER,
        sandbox=True,
    )
    calls = _install_httpx_response(
        monkeypatch,
        _json_response(
            {"code": 40105, "message": "invalid auth_code"},
            url=BUSINESS_SANDBOX_VERIFY_URL,
            status_code=400,
        ),
    )

    response = await verify_app_credentials(ApiType.MARKETING, sandbox=True)

    assert response["valid"] is True
    assert len(calls) == 1
    assert calls[0][0] == BUSINESS_SANDBOX_VERIFY_URL


def _install_httpx_response(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            _ = exc_type, exc, traceback

        async def post(self, url: str, data: dict[str, str]) -> httpx.Response:
            calls.append((url, data))
            return response

    monkeypatch.setattr(app_credentials_tools.httpx, "AsyncClient", FakeAsyncClient)
    return calls


def _json_response(
    payload: dict[str, object],
    *,
    url: str,
    status_code: int = 200,
) -> httpx.Response:
    request = httpx.Request("POST", url)
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(payload).encode("utf-8"),
        request=request,
    )


def _is_recent_datetime(value: str) -> bool:
    verified_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.now(UTC) - verified_at < timedelta(seconds=5)
