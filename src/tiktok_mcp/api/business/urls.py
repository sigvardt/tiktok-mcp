from __future__ import annotations

BUSINESS_PROD_URL = "https://business-api.tiktok.com"
BUSINESS_SANDBOX_URL = "https://sandbox-ads.tiktok.com"
BUSINESS_API_BASE = BUSINESS_PROD_URL

BUSINESS_AUTH_PATH = "/portal/auth"
BUSINESS_ACCESS_TOKEN_PATH = "/open_api/v1.3/oauth2/access_token/"
BUSINESS_REFRESH_TOKEN_PATH = "/open_api/v1.3/oauth2/refresh_token/"
BUSINESS_TT_USER_TOKEN_PATH = "/open_api/v1.3/tt_user/oauth2/token/"
BUSINESS_TT_USER_REFRESH_TOKEN_PATH = "/open_api/v1.3/tt_user/oauth2/refresh_token/"


def business_base_url(sandbox: bool) -> str:
    return BUSINESS_SANDBOX_URL if sandbox else BUSINESS_PROD_URL


def business_url(path: str, *, sandbox: bool) -> str:
    if not path.startswith("/"):
        msg = "Business API paths must start with '/'."
        raise ValueError(msg)
    return f"{business_base_url(sandbox)}{path}"


def business_oauth_url(path: str) -> str:
    if not path.startswith("/"):
        msg = "Business API OAuth paths must start with '/'."
        raise ValueError(msg)
    return f"{BUSINESS_PROD_URL}{path}"


__all__ = [
    "BUSINESS_ACCESS_TOKEN_PATH",
    "BUSINESS_API_BASE",
    "BUSINESS_AUTH_PATH",
    "BUSINESS_PROD_URL",
    "BUSINESS_REFRESH_TOKEN_PATH",
    "BUSINESS_SANDBOX_URL",
    "BUSINESS_TT_USER_REFRESH_TOKEN_PATH",
    "BUSINESS_TT_USER_TOKEN_PATH",
    "business_base_url",
    "business_oauth_url",
    "business_url",
]
