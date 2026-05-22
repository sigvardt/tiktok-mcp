from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.client import HTTPResponse
from pathlib import Path
from typing import TypeAlias, TypedDict, cast


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
TokenExchangeResult: TypeAlias = dict[str, JsonValue]


class RedirectParseResult(TypedDict):
    code: str
    state: str
    host: str
    extra_params: dict[str, str | list[str]]


DISPLAY_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
DISPLAY_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
BUSINESS_AUTH_URL = "https://business-api.tiktok.com/portal/auth"
BUSINESS_TOKEN_URL = "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"

DEFAULT_DISPLAY_SCOPES = ["user.info.basic"]
SENSITIVE_KEY_MARKERS = ("auth", "code", "key", "secret", "token", "verifier")
MARKDOWN_LINK_RE = re.compile(r"^\[[^\]]*\]\((?P<url>.+)\)$")

CREDENTIAL_ENV: dict[str, dict[str, str]] = {
    "display": {
        "app_id": "TIKTOK_S1_DISPLAY_APP_ID",
        "client_secret": "TIKTOK_S1_DISPLAY_CLIENT_SECRET",
        "redirect_uri": "TIKTOK_S1_DISPLAY_REDIRECT_URI",
    },
    "business": {
        "app_id": "TIKTOK_S1_BUSINESS_APP_ID",
        "client_secret": "TIKTOK_S1_BUSINESS_CLIENT_SECRET",
        "redirect_uri": "TIKTOK_S1_BUSINESS_REDIRECT_URI",
    },
}


def build_auth_url(
    api: str,
    app_id: str,
    redirect_uri: str,
    scopes: list[str],
    use_pkce: bool = True,
) -> tuple[str, str, str | None]:
    normalized_api = _normalize_api(api)
    state = secrets.token_urlsafe(32)

    if normalized_api == "display":
        params = {
            "client_key": app_id,
            "scope": ",".join(scopes),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        pkce_verifier = None
        if use_pkce:
            pkce_verifier = secrets.token_urlsafe(64)
            # TikTok Display uses OAuth PKCE S256: SHA-256, URL-safe base64, no padding.
            params["code_challenge"] = _build_pkce_challenge(pkce_verifier)
            params["code_challenge_method"] = "S256"
        return f"{DISPLAY_AUTH_URL}?{urllib.parse.urlencode(params)}", state, pkce_verifier

    params = {
        "app_id": app_id,
        "state": state,
        "redirect_uri": redirect_uri,
    }
    return f"{BUSINESS_AUTH_URL}?{urllib.parse.urlencode(params)}", state, None


def parse_redirect_url(raw: str) -> RedirectParseResult:
    cleaned_url = _clean_pasted_url(raw)
    parsed_url = urllib.parse.urlparse(cleaned_url)

    if not parsed_url.scheme or not parsed_url.netloc:
        raise ValueError("Redirect URL must be an absolute URL with a scheme and host.")

    query_params: dict[str, list[str]] = urllib.parse.parse_qs(
        parsed_url.query,
        keep_blank_values=True,
    )
    if not query_params and parsed_url.fragment:
        query_params = urllib.parse.parse_qs(parsed_url.fragment, keep_blank_values=True)

    code = _first_non_empty(query_params.get("code") or query_params.get("auth_code"), "code")
    state = _first_non_empty(query_params.get("state"), "state")
    host = parsed_url.hostname
    if not host:
        raise ValueError("Redirect URL host could not be parsed.")

    extra_params = {
        name: _collapse_param_values(values)
        for name, values in query_params.items()
        if name not in {"auth_code", "code", "state"}
    }

    return {
        "code": code,
        "state": state,
        "host": host,
        "extra_params": extra_params,
    }


def exchange_code_display(
    app_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    pkce_verifier: str | None,
) -> TokenExchangeResult:
    body_params = {
        "client_key": app_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if pkce_verifier is not None:
        body_params["code_verifier"] = pkce_verifier

    request = urllib.request.Request(
        DISPLAY_TOKEN_URL,
        data=urllib.parse.urlencode(body_params).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    return _open_json(request)


def exchange_code_business(app_id: str, client_secret: str, auth_code: str) -> TokenExchangeResult:
    payload = {
        "app_id": app_id,
        "secret": client_secret,
        "auth_code": auth_code,
    }
    request = urllib.request.Request(
        BUSINESS_TOKEN_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Accept": "application/json",
            # Business API diverges from Display here: TikTok expects JSON, not form data.
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return _open_json(request)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    api = cast(str, args.api)
    action = cast(str, args.action)
    pasted_url = cast(str | None, args.url)
    scopes_arg = cast(list[str] | None, args.scope)
    no_browser = cast(bool, args.no_browser)

    app_id, client_secret, redirect_uri = _load_credentials(api, need_secret=action == "exchange")

    if action == "open":
        scopes = scopes_arg or DEFAULT_DISPLAY_SCOPES
        auth_url, state, pkce_verifier = build_auth_url(
            api,
            app_id,
            redirect_uri,
            scopes,
            use_pkce=api == "display",
        )
        session_path = _save_session(api, state, pkce_verifier)

        print("authorization_url:")
        print(auth_url)
        print(f"state: {_redact_value(state)}")
        if pkce_verifier is not None:
            print(f"pkce_verifier: {_redact_value(pkce_verifier)}")
        print(f"session_file: {session_path}")

        if no_browser:
            print("browser_opened: skipped (--no-browser)")
            return

        opened = webbrowser.open(auth_url)
        print(f"browser_opened: {str(opened).lower()}")
        if not opened:
            print("Copy authorization_url manually; browser launch is fire-and-forget.")
        return

    if not pasted_url:
        raise SystemExit("--url is required when --action exchange is used.")

    parsed_redirect = parse_redirect_url(pasted_url)
    _assert_redirect_host_matches(redirect_uri, parsed_redirect["host"])
    session = _load_session(api)
    _assert_state_matches(session, parsed_redirect["state"])

    print(f"host: {parsed_redirect['host']}")
    print(f"code: {_redact_value(parsed_redirect['code'])}")
    print(f"state: {_redact_value(parsed_redirect['state'])}")
    print("extra_params_redacted:")
    print(
        json.dumps(
            redact_json_for_print(cast(JsonValue, parsed_redirect["extra_params"]), force=True),
            indent=2,
        )
    )

    if api == "display":
        pkce_verifier = session.get("pkce_verifier")
        if not isinstance(pkce_verifier, str) or not pkce_verifier:
            raise SystemExit("Missing PKCE verifier; rerun --action open on this machine first.")
        result = exchange_code_display(
            app_id,
            client_secret,
            parsed_redirect["code"],
            redirect_uri,
            pkce_verifier,
        )
    else:
        result = exchange_code_business(app_id, client_secret, parsed_redirect["code"])

    print("token_exchange_result_redacted:")
    print(json.dumps(redact_json_for_print(result), indent=2, sort_keys=True))
    print(f"token_exchange_success: {str(_looks_successful_exchange(result)).lower()}")


def _build_pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _clean_pasted_url(raw: str) -> str:
    cleaned_url = raw.strip()
    markdown_match = MARKDOWN_LINK_RE.match(cleaned_url)
    if markdown_match:
        cleaned_url = markdown_match.group("url").strip()

    while len(cleaned_url) >= 2 and cleaned_url[0] == cleaned_url[-1] and cleaned_url[0] in {
        '"',
        "`",
    }:
        cleaned_url = cleaned_url[1:-1].strip()

    return cleaned_url


def _first_non_empty(values: list[str] | None, param_name: str) -> str:
    for value in values or []:
        if value:
            return value
    raise ValueError(f"Redirect URL is missing a non-empty {param_name} parameter.")


def _collapse_param_values(values: list[str]) -> str | list[str]:
    if len(values) == 1:
        return values[0]
    return values


def _open_json(request: urllib.request.Request) -> TokenExchangeResult:
    try:
        response = cast(HTTPResponse, urllib.request.urlopen(request, timeout=30))
        try:
            response_body = response.read()
            payload = _decode_json_response(response_body)
            http_status = response.status
        finally:
            response.close()
        if isinstance(payload, dict):
            return {"http_status": http_status, **payload}
        return {"http_status": http_status, "response": payload}
    except urllib.error.HTTPError as error:
        payload = _decode_json_response(error.read())
        return {"http_status": error.code, "error": payload}


def _decode_json_response(response_body: bytes) -> JsonValue:
    if not response_body:
        return {}
    response_text = response_body.decode(errors="replace")
    try:
        return cast(JsonValue, json.loads(response_text))
    except json.JSONDecodeError:
        return {"raw_response": _redact_value(response_text)}


def _normalize_api(api: str) -> str:
    if api not in {"business", "display"}:
        raise ValueError("api must be 'display' or 'business'.")
    return api


def _build_parser() -> argparse.ArgumentParser:
    examples = """
Examples:
  python spikes/s1_redirect.py --api display --action open
  python spikes/s1_redirect.py --api display --action open --no-browser
  python spikes/s1_redirect.py --api display --action exchange --url '<PASTE_FULL_REDIRECT_URL>'
  python spikes/s1_redirect.py --api business --action open --no-browser
  python spikes/s1_redirect.py --api business --action exchange --url '<PASTE_FULL_REDIRECT_URL>'

Display env vars:
  TIKTOK_S1_DISPLAY_APP_ID
  TIKTOK_S1_DISPLAY_CLIENT_SECRET
  TIKTOK_S1_DISPLAY_REDIRECT_URI

Business env vars:
  TIKTOK_S1_BUSINESS_APP_ID
  TIKTOK_S1_BUSINESS_CLIENT_SECRET
  TIKTOK_S1_BUSINESS_REDIRECT_URI
"""
    parser = argparse.ArgumentParser(
        description="Spike S1 manual-paste OAuth redirect fidelity helper for TikTok sandbox apps.",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument("--api", choices=("display", "business"), required=True)
    _ = parser.add_argument("--action", choices=("open", "exchange"), required=True)
    _ = parser.add_argument("--url", help="Full redirect URL pasted from the browser address bar.")
    _ = parser.add_argument(
        "--scope",
        action="append",
        help="Display API scope; repeat for multiple scopes. Defaults to user.info.basic.",
    )
    _ = parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the auth URL without calling webbrowser.open, useful over SSH/headless shells.",
    )
    return parser


def _load_credentials(api: str, need_secret: bool) -> tuple[str, str, str]:
    env_names = CREDENTIAL_ENV[_normalize_api(api)]
    _assert_sandbox_app_id_env_name(env_names["app_id"])
    app_id = _required_env(env_names["app_id"])
    redirect_uri = _required_env(env_names["redirect_uri"])
    client_secret = _required_env(env_names["client_secret"]) if need_secret else ""
    return app_id, client_secret, redirect_uri


def _assert_sandbox_app_id_env_name(app_id_env_name: str) -> None:
    if "PROD" in app_id_env_name.upper():
        raise SystemExit("Sandbox credentials only; refusing production credentials.")


def _required_env(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {env_name}")
    return value


def _session_file(api: str) -> Path:
    return Path(tempfile.gettempdir()) / f"tiktok_mcp_s1_redirect_{_normalize_api(api)}.json"


def _save_session(api: str, state: str, pkce_verifier: str | None) -> Path:
    session_path = _session_file(api)
    session_data = {
        "state": state,
        "pkce_verifier": pkce_verifier,
        "created_at": int(time.time()),
    }
    _ = session_path.write_text(json.dumps(session_data, indent=2), encoding="utf-8")
    try:
        session_path.chmod(0o600)
    except OSError:
        pass
    return session_path


def _load_session(api: str) -> dict[str, JsonValue]:
    session_path = _session_file(api)
    if not session_path.exists():
        raise SystemExit("Missing S1 session file; rerun --action open on this machine first.")
    try:
        session = cast(JsonValue, json.loads(session_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Unreadable S1 session file: {session_path}") from exc
    if not isinstance(session, dict):
        raise SystemExit(f"Unexpected S1 session file shape: {session_path}")
    return session


def _assert_state_matches(session: dict[str, JsonValue], parsed_state: str) -> None:
    expected_state = session.get("state")
    if not isinstance(expected_state, str) or not expected_state:
        raise SystemExit("S1 session file is missing state; rerun --action open.")
    if not secrets.compare_digest(expected_state, parsed_state):
        raise SystemExit("Parsed state does not match the latest --action open state.")


def _assert_redirect_host_matches(registered_redirect_uri: str, parsed_host: str) -> None:
    registered_host = urllib.parse.urlparse(registered_redirect_uri).hostname
    if not registered_host:
        raise SystemExit("Registered redirect URI env var must be an absolute URL with a host.")
    if registered_host != parsed_host:
        raise SystemExit(
            f"Redirect host mismatch: parsed {parsed_host!r}, expected {registered_host!r}."
        )


def _looks_successful_exchange(payload: TokenExchangeResult) -> bool:
    http_status = payload.get("http_status")
    if isinstance(http_status, int) and not 200 <= http_status < 300:
        return False
    if "error" in payload:
        return False
    return _contains_key(payload, "access_token")


def _contains_key(value: JsonValue, target_key: str) -> bool:
    if isinstance(value, dict):
        return any(key == target_key or _contains_key(child, target_key) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_key(child, target_key) for child in value)
    return False


def _redact_value(value: str | None) -> str:
    if value is None:
        return "<none>"
    return f"{value[:4]}…(len={len(value)})"


def redact_json_for_print(value: JsonValue, force: bool = False) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: redact_json_for_print(child, force or _is_sensitive_key(str(key)))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_json_for_print(child, force) for child in value]
    if isinstance(value, str) and (force or _looks_token_like(value)):
        return _redact_value(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered_key = key.lower()
    return any(marker in lowered_key for marker in SENSITIVE_KEY_MARKERS)


def _looks_token_like(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{40,}", value))


if __name__ == "__main__":
    _ = main()
