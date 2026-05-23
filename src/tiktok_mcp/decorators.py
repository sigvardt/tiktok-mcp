"""Decorators for write and account-change guards."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
from collections.abc import Awaitable, Callable, Iterable, Iterator
from functools import wraps
from types import ModuleType
from typing import Any, Literal, ParamSpec, Protocol, TypeVar, cast

WriteApiName = Literal["display", "marketing", "comments", "posting"]

_VALID_API_NAMESPACES: frozenset[str] = frozenset(
    {"display", "marketing", "comments", "posting"}
)
_LIVE_ACCOUNT_SAFETY_ENV = "TIKTOK_MCP_LIVE_ACCOUNT_SAFETY"
# Secure by default: unset TIKTOK_MCP_LIVE_ACCOUNT_SAFETY locks every live write
# surface so no OAuthed account can mutate live TikTok state by mistake.
_DEFAULT_LIVE_ACCOUNT_SAFETY_APIS: frozenset[str] = frozenset(_VALID_API_NAMESPACES)
_FALSE_VALUES: frozenset[str] = frozenset({"", "0", "false", "no"})
_TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "all"})
_WRITE_PREFIXES: tuple[str, ...] = (
    "create_",
    "update_",
    "delete_",
    "pause_",
    "resume_",
    "upload_",
    "post_",
    "pin_",
    "unpin_",
    "hide_",
    "unhide_",
    "revoke_",
    "refresh_",
    "publish_",
    "move_",
    "finalize_",
    "cancel_",
)
_ACCOUNT_CHANGE_NAMES: frozenset[str] = frozenset(
    {
        "add_account",
        "add_account_with_loopback",
        "complete_account_login",
        "remove_account",
        "rename_account",
        "set_app_credentials",
    }
)
_READ_PREFIXES: tuple[str, ...] = (
    "get_",
    "list_",
    "describe_",
    "search_",
    "verify_",
)

_Params = ParamSpec("_Params")
_ReturnT = TypeVar("_ReturnT")
_CallableT = TypeVar("_CallableT", bound=Callable[..., object])


class _DestructiveMarker(Protocol):
    __tiktok_mcp_destructive__: bool


class _WriteApiMarker(Protocol):
    __tiktok_mcp_write_api__: str


class _AccountChangeMarker(Protocol):
    __tiktok_mcp_account_change__: bool


class _ReadOnlyMarker(Protocol):
    __tiktok_mcp_read_only__: bool

logger = logging.getLogger(__name__)


def parse_writes_env(value: str | None) -> set[str]:
    if value is None:
        return set()

    normalized_value = value.strip().lower()
    if normalized_value in _FALSE_VALUES:
        return set()
    if normalized_value in _TRUE_VALUES:
        return set(_VALID_API_NAMESPACES)

    enabled_namespaces: set[str] = set()
    for token in value.split(","):
        normalized_token = token.strip().lower()
        if not normalized_token:
            continue
        if normalized_token in _VALID_API_NAMESPACES:
            enabled_namespaces.add(normalized_token)
        elif normalized_token == "all":
            enabled_namespaces.update(_VALID_API_NAMESPACES)
        else:
            logger.warning(
                "Unknown TIKTOK_MCP_ALLOW_WRITES token %r ignored",
                normalized_token,
            )

    return enabled_namespaces


def writes_enabled_for(api: str, env_value: str | None = None) -> bool:
    if api not in _VALID_API_NAMESPACES:
        raise ValueError(f"Unknown TikTok MCP write API namespace: {api!r}")

    value = os.environ.get("TIKTOK_MCP_ALLOW_WRITES") if env_value is None else env_value
    return api in parse_writes_env(value)


def parse_live_account_safety_env(value: str | None) -> set[str]:
    if value is None:
        return set(_DEFAULT_LIVE_ACCOUNT_SAFETY_APIS)

    normalized_value = value.strip().lower()
    if normalized_value in _FALSE_VALUES:
        return set()
    if normalized_value in _TRUE_VALUES:
        return set(_VALID_API_NAMESPACES)

    locked_namespaces: set[str] = set()
    for token in value.split(","):
        normalized_token = token.strip().lower()
        if not normalized_token:
            continue
        if normalized_token in _VALID_API_NAMESPACES:
            locked_namespaces.add(normalized_token)
        elif normalized_token == "all":
            locked_namespaces.update(_VALID_API_NAMESPACES)
        else:
            logger.warning(
                "Unknown %s token %r ignored",
                _LIVE_ACCOUNT_SAFETY_ENV,
                normalized_token,
            )

    return locked_namespaces


def live_account_safety_locked_for(api: str, env_value: str | None = None) -> bool:
    if api not in _VALID_API_NAMESPACES:
        raise ValueError(f"Unknown TikTok MCP write API namespace: {api!r}")

    value = os.environ.get(_LIVE_ACCOUNT_SAFETY_ENV) if env_value is None else env_value
    return api in parse_live_account_safety_env(value)


def parse_account_changes_env(value: str | None) -> bool:
    if value is None:
        return False

    normalized_value = value.strip().lower()
    if normalized_value in _FALSE_VALUES:
        return False
    return normalized_value in _TRUE_VALUES


def account_changes_enabled(env_value: str | None = None) -> bool:
    value = (
        os.environ.get("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES")
        if env_value is None
        else env_value
    )
    return parse_account_changes_env(value)


def require_writes_enabled(
    api: WriteApiName,
) -> Callable[
    [Callable[_Params, Awaitable[_ReturnT]]],
    Callable[_Params, Awaitable[_ReturnT | dict[str, Any]]],
]:
    if api not in _VALID_API_NAMESPACES:
        raise ValueError(f"Unknown TikTok MCP write API namespace: {api!r}")

    def decorator(
        fn: Callable[_Params, Awaitable[_ReturnT]],
    ) -> Callable[_Params, Awaitable[_ReturnT | dict[str, Any]]]:
        _mark_write_tool(fn, api)

        @wraps(fn)
        async def wrapper(
            *args: _Params.args,
            **kwargs: _Params.kwargs,
        ) -> _ReturnT | dict[str, Any]:
            if live_account_safety_locked_for(api):
                logger.info(
                    "Live-account safety locked %s tool %s",
                    api,
                    fn.__name__,
                )
                return _live_account_safety_locked_error(fn, api)

            if writes_enabled_for(api):
                return await fn(*args, **kwargs)

            return _writes_disabled_error(fn, api)

        _mark_write_tool(wrapper, api)
        return wrapper

    return decorator


def require_account_changes_enabled(
    fn: Callable[_Params, Awaitable[_ReturnT]],
) -> Callable[_Params, Awaitable[_ReturnT | dict[str, Any]]]:
    _mark_account_change_tool(fn)

    @wraps(fn)
    async def wrapper(
        *args: _Params.args,
        **kwargs: _Params.kwargs,
    ) -> _ReturnT | dict[str, Any]:
        if account_changes_enabled():
            return await fn(*args, **kwargs)

        return _account_changes_disabled_error(fn)

    _mark_account_change_tool(wrapper)
    return wrapper


def is_destructive(fn: Callable[..., object]) -> bool:
    return bool(getattr(fn, "__tiktok_mcp_destructive__", False))


def mark_read_only(fn: _CallableT) -> _CallableT:
    cast(_ReadOnlyMarker, fn).__tiktok_mcp_read_only__ = True
    return fn


def assert_tool_decoration_compliance(module_path: str) -> list[str]:
    violations: list[str] = []

    for module in _walk_modules(module_path):
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if fn.__module__ != module.__name__:
                continue
            if name in _ACCOUNT_CHANGE_NAMES:
                if not bool(getattr(fn, "__tiktok_mcp_account_change__", False)):
                    violations.append(
                        f"{module.__name__}.{name} is an account-change tool but is missing "
                        + "@require_account_changes_enabled/destructiveHint metadata "
                        + "(__tiktok_mcp_account_change__=True)"
                    )
                continue
            if name.startswith(_WRITE_PREFIXES):
                if not is_destructive(fn):
                    violations.append(
                        f"{module.__name__}.{name} matches a destructive naming pattern but "
                        + "is missing @require_writes_enabled(...)/destructiveHint metadata "
                        + "(__tiktok_mcp_destructive__=True)"
                    )
                continue
            if name.startswith(_READ_PREFIXES) and not bool(
                getattr(fn, "__tiktok_mcp_read_only__", False)
            ):
                violations.append(
                    f"{module.__name__}.{name} matches a read-only naming pattern but is missing "
                    + "@mark_read_only metadata (__tiktok_mcp_read_only__=True)"
                )

    return violations


def _writes_disabled_error(fn: Callable[..., object], api: str) -> dict[str, Any]:
    return {
        "error": "writes_disabled",
        "message": (
            f"Write/delete tools for '{api}' are disabled. "
            f"Set TIKTOK_MCP_ALLOW_WRITES=all (or include '{api}') to enable."
        ),
        "tool": fn.__name__,
        "api": api,
        "would_have_done": getattr(fn, "__tiktok_mcp_summary__", f"{fn.__name__}(...)")
    }


def _live_account_safety_locked_error(fn: Callable[..., object], api: str) -> dict[str, Any]:
    env_value = os.environ.get(_LIVE_ACCOUNT_SAFETY_ENV)
    reason = (
        f"{_LIVE_ACCOUNT_SAFETY_ENV} is unset, so its secure default locks all live API surfaces."
        if env_value is None
        else f"{_LIVE_ACCOUNT_SAFETY_ENV} includes '{api}'."
    )
    return {
        "error": "live_account_safety_locked",
        "message": (
            f"Destructive tools for '{api}' are locked by {_LIVE_ACCOUNT_SAFETY_ENV} "
            "before client construction or HTTP work."
        ),
        "reason": reason,
        "unlock_hint": (
            f"set {_LIVE_ACCOUNT_SAFETY_ENV}= or "
            f"{_LIVE_ACCOUNT_SAFETY_ENV}=<api_csv_without_this_api>"
        ),
        "tool": fn.__name__,
        "api": api,
        "would_have_done": getattr(fn, "__tiktok_mcp_summary__", f"{fn.__name__}(...)")
    }


def _account_changes_disabled_error(fn: Callable[..., object]) -> dict[str, Any]:
    return {
        "error": "account_changes_disabled",
        "message": (
            "Account-change tools are disabled. "
            "Set TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES=1 (or true/yes) to enable."
        ),
        "tool": fn.__name__,
        "api": "account_changes",
        "would_have_done": getattr(fn, "__tiktok_mcp_summary__", f"{fn.__name__}(...)")
    }


def _mark_write_tool(fn: _CallableT, api: str) -> _CallableT:
    cast(_DestructiveMarker, fn).__tiktok_mcp_destructive__ = True
    cast(_WriteApiMarker, fn).__tiktok_mcp_write_api__ = api
    return fn


def _mark_account_change_tool(fn: _CallableT) -> _CallableT:
    cast(_DestructiveMarker, fn).__tiktok_mcp_destructive__ = True
    cast(_AccountChangeMarker, fn).__tiktok_mcp_account_change__ = True
    return fn


def _walk_modules(module_path: str) -> Iterator[ModuleType]:
    module = importlib.import_module(module_path)
    yield module

    package_paths = getattr(module, "__path__", None)
    if package_paths is None:
        return

    for module_info in pkgutil.walk_packages(
        cast(Iterable[str], package_paths),
        f"{module.__name__}.",
    ):
        yield importlib.import_module(module_info.name)


__all__ = [
    "_VALID_API_NAMESPACES",
    "account_changes_enabled",
    "assert_tool_decoration_compliance",
    "is_destructive",
    "live_account_safety_locked_for",
    "mark_read_only",
    "parse_account_changes_env",
    "parse_live_account_safety_env",
    "parse_writes_env",
    "require_account_changes_enabled",
    "require_writes_enabled",
    "writes_enabled_for",
]
