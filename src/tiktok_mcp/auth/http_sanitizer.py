"""Safe httpx status handling that strips response bodies from exceptions."""

from __future__ import annotations

from typing import cast

import httpx
from typing_extensions import override


class SanitizedHttpxError(Exception):
    """HTTP status error with only safe TikTok request context."""

    def __init__(
        self,
        status: int,
        url_path: str,
        tiktok_message: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status: int = status
        self.url_path: str = url_path
        self.tiktok_message: str | None = tiktok_message
        self.request_id: str | None = request_id
        super().__init__(str(self))

    @override
    def __str__(self) -> str:
        parts = [f"status={self.status}", f"url_path={self.url_path}"]
        if self.request_id is not None:
            parts.append(f"request_id={self.request_id}")
        return "TikTok HTTP error (" + ", ".join(parts) + ")"


async def safe_raise_for_status(response: httpx.Response) -> None:
    """Raise a sanitized error for HTTP 4xx/5xx responses.

    Business API `HTTP 200 + code != 0` envelope handling belongs to Wave 1 T7's
    `decode_business_response`; this hook only handles unsafe HTTP status errors.
    """
    if response.status_code >= 400:
        request_id = cast(str | None, response.headers.get("x-tt-logid"))
        raise SanitizedHttpxError(
            status=response.status_code,
            url_path=response.request.url.path,
            tiktok_message=None,
            request_id=request_id,
        )


def install_httpx_sanitization(client: httpx.AsyncClient) -> None:
    response_hooks = client.event_hooks.setdefault("response", [])
    for hook in response_hooks:
        if "safe_raise_for_status" in getattr(hook, "__qualname__", ""):
            return
    response_hooks.append(safe_raise_for_status)
