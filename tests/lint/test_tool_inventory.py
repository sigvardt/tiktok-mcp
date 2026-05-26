"""T36 lint audit for the live MCP tool inventory.

This audit checks the registered FastMCP tool surface against the shipped name
and annotation conventions. The current surface has a few intentional naming
exceptions that are part of the v0.1 contract:
- ``display_get_user_info`` is the canonical Display read tool.
- ``display_query_videos`` uses ``query_`` as a read verb.
- ``marketing_run_sync_report``, ``marketing_run_async_report``,
  ``marketing_poll_async_report``, and ``marketing_download_async_report`` are
  read-only report tools.
- ``init_video_upload`` and ``finalize_video_upload`` are write tools in the
  posting upload flow.
- ``get_publish_status`` is INTENTIONAL: it is write-gated even though it
  begins with ``get_`` because ``publish`` is a write fragment.

Plan coverage uses the plan's blocked evidence lines. Some tasks spell the
blocked case as ``Expected Result: ... writes_disabled`` while others use the
acceptance-criteria test name ``test_blocked``; the helper treats either as a
blocked-plan signal so the audit still cross-checks the shipped write surface.
For write tools whose plan section only states the gate declaratively, the
helper also accepts the nearby ``@require_writes_enabled(...)`` declaration as
coverage evidence.
"""

# pyright: reportMissingTypeStubs=false
from __future__ import annotations

import json
import re
from collections import Counter

from tiktok_mcp.internal.tool_registry import (
    list_all_tools_with_annotations,
    write_tools_with_writes_disabled_plan_coverage,
)

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]+$")
SURFACE_PREFIXES = {"display", "marketing", "comments", "posting"}
READ_PREFIXES = ("get", "list", "describe", "search", "verify", "query")
READ_NAME_EXCEPTIONS = {
    "marketing_run_sync_report",
    "marketing_run_async_report",
    "marketing_poll_async_report",
    "marketing_download_async_report",
}
WRITE_PREFIXES = (
    "create",
    "update",
    "delete",
    "post",
    "move",
    "pin",
    "unpin",
    "hide",
    "unhide",
    "upload",
    "cancel",
    "revoke",
    "refresh",
    "init",
    "finalize",
)
WRITE_NAME_EXCEPTIONS = {"get_publish_status"}
ACCOUNT_CHANGE_NAMES = {
    "add_account",
    "add_account_with_loopback",
    "complete_account_login",
    "remove_account",
    "rename_account",
    "set_app_credentials",
}


def test_tool_inventory_matches_name_and_annotation_rules() -> None:
    inventory = list_all_tools_with_annotations()
    assert len(inventory) >= 40, f"expected at least 40 tools, found {len(inventory)}"

    names = [entry["name"] for entry in inventory]
    duplicate_names = [name for name, count in Counter(names).items() if count > 1]
    assert not duplicate_names, f"duplicate tool name '{duplicate_names[0]}'"
    assert json.dumps(inventory, indent=2)

    for entry in inventory:
        name = entry["name"]
        assert isinstance(name, str)
        assert TOOL_NAME_RE.fullmatch(name), f"tool '{name}' violates snake_case"

        read_only_hint = bool(entry["readOnlyHint"])
        destructive_hint = bool(entry["destructiveHint"])
        assert read_only_hint != destructive_hint, (
            f"tool '{name}' must set exactly one of readOnlyHint/destructiveHint"
        )

        module = entry["module"]
        assert isinstance(module, str) and module.startswith("tiktok_mcp.tools."), (
            f"tool '{name}' has invalid module {module!r}"
        )

        if name in ACCOUNT_CHANGE_NAMES:
            assert destructive_hint, f"tool '{name}' missing destructiveHint=True"
            assert entry["has_account_changes_gate_decorator"], (
                f"tool '{name}' missing account-change decorator"
            )
            assert not entry["has_write_gate_decorator"], (
                f"tool '{name}' must not use write-gate decorator"
            )
            continue

        if _is_write_name(name):
            assert destructive_hint, f"tool '{name}' missing destructiveHint=True"
            assert entry["has_write_gate_decorator"], f"tool '{name}' missing write-gate decorator"
            assert not entry["has_account_changes_gate_decorator"], (
                f"tool '{name}' must not use account-change decorator"
            )
            continue

        if _is_read_name(name):
            assert read_only_hint, f"tool '{name}' missing readOnlyHint=True"
            assert not entry["has_write_gate_decorator"], (
                f"tool '{name}' must not use write-gate decorator"
            )
            assert not entry["has_account_changes_gate_decorator"], (
                f"tool '{name}' must not use account-change decorator"
            )
            continue

        raise AssertionError(f"tool '{name}' does not match a supported naming rule")


def test_write_tools_have_blocked_plan_coverage() -> None:
    inventory = list_all_tools_with_annotations()
    write_tool_names = {
        entry["name"] for entry in inventory if entry["has_write_gate_decorator"] is True
    }
    covered_tool_names = write_tools_with_writes_disabled_plan_coverage()

    assert covered_tool_names == write_tool_names, (
        "missing blocked-plan evidence for write tools: "
        f"{sorted(write_tool_names - covered_tool_names)}"
    )


def _tool_name_body(name: str) -> str:
    surface, separator, remainder = name.partition("_")
    if surface in SURFACE_PREFIXES and separator and remainder:
        return remainder
    return name


def _is_read_name(name: str) -> bool:
    body = _tool_name_body(name)
    return _has_action_prefix(body, READ_PREFIXES) or name in READ_NAME_EXCEPTIONS


def _is_write_name(name: str) -> bool:
    body = _tool_name_body(name)
    return (
        _has_action_prefix(body, WRITE_PREFIXES)
        or "publish" in body
        or name in WRITE_NAME_EXCEPTIONS
    )


def _has_action_prefix(body: str, prefixes: tuple[str, ...]) -> bool:
    return any(body == prefix or body.startswith(f"{prefix}_") for prefix in prefixes)
