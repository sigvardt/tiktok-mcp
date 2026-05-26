"""Manual-paste OAuth redirect URL parser lifted from spike S1.

The parser originated in ``spikes/s1_redirect.py`` and preserves the spike's
robust handling for browser address-bar pastes, quoted strings, backticks, and
Markdown links.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import TypedDict


class RedirectParseResult(TypedDict):
    code: str
    state: str
    host: str
    extra_params: dict[str, list[str]]


MARKDOWN_LINK_RE = re.compile(r"^\[[^\]]*\]\((?P<url>.+)\)$")


def parse_redirect_url(raw: str) -> RedirectParseResult:
    cleaned_url = _clean_pasted_url(raw)
    parsed_url = urllib.parse.urlparse(cleaned_url)

    if not parsed_url.scheme or not parsed_url.netloc:
        msg = "Redirect URL must be an absolute URL with a scheme and host."
        raise ValueError(msg)

    query_params = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
    if not query_params and parsed_url.fragment:
        query_params = urllib.parse.parse_qs(parsed_url.fragment, keep_blank_values=True)

    code = _first_non_empty(query_params.get("code") or query_params.get("auth_code"), "code")
    state = _first_non_empty(query_params.get("state"), "state")
    host = parsed_url.hostname
    if not host:
        msg = "Redirect URL host could not be parsed."
        raise ValueError(msg)

    extra_params = {
        name: values
        for name, values in query_params.items()
        if name not in {"auth_code", "code", "state"}
    }

    return {"code": code, "state": state, "host": host, "extra_params": extra_params}


def _clean_pasted_url(raw: str) -> str:
    cleaned_url = raw.strip()
    markdown_match = MARKDOWN_LINK_RE.match(cleaned_url)
    if markdown_match:
        cleaned_url = markdown_match.group("url").strip()

    while (
        len(cleaned_url) >= 2 and cleaned_url[0] == cleaned_url[-1] and cleaned_url[0] in {'"', "`"}
    ):
        cleaned_url = cleaned_url[1:-1].strip()

    return cleaned_url


def _first_non_empty(values: list[str] | None, param_name: str) -> str:
    for value in values or []:
        if value:
            return value
    msg = f"Redirect URL is missing a non-empty {param_name} parameter."
    raise ValueError(msg)


__all__ = ["RedirectParseResult", "parse_redirect_url"]
