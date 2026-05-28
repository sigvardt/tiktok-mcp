"""Registered MCP tool inventory helpers for the T36 audit.

The live FastMCP registry is the source of truth, and the shipped surface has a
few intentional naming exceptions that the audit must understand.
"""

# pyright: reportAny=false, reportExplicitAny=false, reportMissingTypeStubs=false, reportPrivateUsage=false, reportUnnecessaryCast=false
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TypedDict, cast

from mcp.server.fastmcp.tools.base import Tool

from tiktok_mcp.server import _register_components, app

_PLAN_PATH = Path(__file__).resolve().parents[3] / ".omo/plans/tiktok-mcp.md"
_SURFACE_PREFIXES = frozenset({"display", "marketing", "comments", "posting"})
_TOOL_MARKER_ATTRIBUTES = (
    "__tiktok_mcp_read_only__",
    "__tiktok_mcp_destructive__",
    "__tiktok_mcp_account_change__",
)
_READ_PREFIXES = ("get_", "list_", "describe_", "search_", "verify_", "query_")
_READ_NAME_EXCEPTIONS = frozenset(
    {
        "marketing_run_sync_report",
        "marketing_run_async_report",
        "marketing_poll_async_report",
        "marketing_download_async_report",
    }
)
_WRITE_PREFIXES = (
    "create_",
    "update_",
    "delete_",
    "post_",
    "move_",
    "pin_",
    "unpin_",
    "hide_",
    "unhide_",
    "upload_",
    "cancel_",
    "revoke_",
    "refresh_",
    "init_",
    "finalize_",
)
_WRITE_NAME_EXCEPTIONS = frozenset[str]()
_ACCOUNT_CHANGE_NAMES = frozenset(
    {
        "add_account",
        "add_account_with_loopback",
        "complete_account_login",
        "poll_loopback_login",
        "remove_account",
        "rename_account",
        "set_app_credentials",
    }
)
_TASK_HEADING_RE = re.compile(r"^- \[(?: |x|~|-)\] \d+\.")


class ToolInventoryEntry(TypedDict):
    name: str
    readOnlyHint: bool
    destructiveHint: bool
    has_write_gate_decorator: bool
    has_account_changes_gate_decorator: bool
    module: str


def list_all_tools_with_annotations() -> list[ToolInventoryEntry]:
    _register_components()
    source_functions = _discover_tool_functions()
    _assert_tool_name_uniqueness(source_functions)

    tool_manager = cast(Any, app._tool_manager)
    registry = cast(dict[str, Tool], tool_manager._tools)
    registry_names = set(registry)
    source_names = {fn.__name__ for fn in source_functions}
    missing_from_registry = sorted(source_names - registry_names)
    extra_in_registry = sorted(registry_names - source_names)
    if missing_from_registry or extra_in_registry:
        raise AssertionError(
            f"registry/source mismatch: missing={missing_from_registry} extra={extra_in_registry}"
        )

    inventory: list[ToolInventoryEntry] = []
    for tool_name in sorted(registry):
        tool = cast(Any, registry[tool_name])
        fn = cast(Any, tool.fn)
        annotations = tool.annotations
        inventory.append(
            {
                "name": tool_name,
                "readOnlyHint": bool(getattr(annotations, "readOnlyHint", False)),
                "destructiveHint": bool(getattr(annotations, "destructiveHint", False)),
                "has_write_gate_decorator": bool(getattr(fn, "__tiktok_mcp_write_api__", "")),
                "has_account_changes_gate_decorator": bool(
                    getattr(fn, "__tiktok_mcp_account_change__", False)
                ),
                "module": fn.__module__,
            }
        )

    return inventory


def write_tools_with_writes_disabled_plan_coverage() -> set[str]:
    write_tool_names = {
        entry["name"]
        for entry in list_all_tools_with_annotations()
        if entry["has_write_gate_decorator"] is True
    }
    plan_text = _PLAN_PATH.read_text(encoding="utf-8")
    covered: set[str] = set()
    for section in _iter_task_sections(plan_text):
        if (
            "writes_disabled" not in section
            and "test_blocked" not in section
            and "@require_writes_enabled(" not in section
        ):
            continue
        for tool_name in write_tool_names:
            if tool_name in section:
                covered.add(tool_name)
    return covered


def _discover_tool_functions() -> list[Any]:
    functions: list[Any] = []
    for module in _walk_tool_modules():
        for _, fn in inspect.getmembers(module, inspect.isfunction):
            if fn.__module__ != module.__name__:
                continue
            if not any(getattr(fn, marker, False) for marker in _TOOL_MARKER_ATTRIBUTES):
                continue
            functions.append(fn)
    return functions


def _walk_tool_modules() -> Iterator[Any]:
    package = importlib.import_module("tiktok_mcp.tools")
    yield package

    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return

    for module_info in pkgutil.walk_packages(
        cast(Iterable[str], package_paths),
        f"{package.__name__}.",
    ):
        yield importlib.import_module(module_info.name)


def _assert_tool_name_uniqueness(tool_functions: list[Any]) -> None:
    counts = Counter(fn.__name__ for fn in tool_functions)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise AssertionError(f"duplicate tool name '{duplicates[0]}'")


def _iter_task_sections(plan_text: str) -> Iterator[str]:
    section_lines: list[str] = []
    in_task_section = False
    for line in plan_text.splitlines():
        if _TASK_HEADING_RE.match(line):
            if section_lines:
                yield "\n".join(section_lines)
            section_lines = [line]
            in_task_section = True
            continue
        if in_task_section:
            section_lines.append(line)
    if section_lines:
        yield "\n".join(section_lines)


__all__ = [
    "list_all_tools_with_annotations",
    "write_tools_with_writes_disabled_plan_coverage",
]
