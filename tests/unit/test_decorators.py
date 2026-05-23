from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

import pytest

import tiktok_mcp.decorators as decorators
from tiktok_mcp.decorators import (
    _VALID_API_NAMESPACES,
    account_changes_enabled,
    assert_tool_decoration_compliance,
    is_destructive,
    parse_account_changes_env,
    parse_writes_env,
    require_account_changes_enabled,
    require_writes_enabled,
    writes_enabled_for,
)
from tiktok_mcp.tools import display_read as display_read_tools
from tiktok_mcp.types.accounts import Account, AccountStatus, ApiType

ALL_APIS = set(_VALID_API_NAMESPACES)
TRUTH_TABLE_CASES: tuple[tuple[str | None, set[str]], ...] = (
    (None, set()),
    ("", set()),
    ("0", set()),
    ("false", set()),
    ("False", set()),
    ("no", set()),
    ("No", set()),
    ("NO", set()),
    ("1", ALL_APIS),
    ("true", ALL_APIS),
    ("True", ALL_APIS),
    ("yes", ALL_APIS),
    ("Yes", ALL_APIS),
    ("YES", ALL_APIS),
    ("all", ALL_APIS),
    ("ALL", ALL_APIS),
    ("marketing", {"marketing"}),
    ("marketing,comments", {"marketing", "comments"}),
    ("all,foo", ALL_APIS),
    ("posting,display,unknown,marketing", {"posting", "display", "marketing"}),
)
class _DecoratedWriteTool(Protocol):
    __tiktok_mcp_destructive__: bool
    __tiktok_mcp_write_api__: str


class _SummaryTool(Protocol):
    __tiktok_mcp_summary__: str


class _FakeDisplayClient:
    def __init__(self) -> None:
        self.closed: bool = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("value", "expected"), TRUTH_TABLE_CASES)
def test_writes_env_truth_table(value: str | None, expected: set[str]) -> None:
    """Write env parsing follows the full truth table."""
    assert parse_writes_env(value) == expected


def test_writes_env_unknown_token_logged_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown write env tokens are warning-logged and ignored."""
    with caplog.at_level(logging.WARNING, logger="tiktok_mcp.decorators"):
        parsed_value = parse_writes_env("marketing,foo,bar")

    assert parsed_value == {"marketing"}
    messages = [record.getMessage() for record in caplog.records]
    assert "Unknown TIKTOK_MCP_ALLOW_WRITES token 'foo' ignored" in messages
    assert "Unknown TIKTOK_MCP_ALLOW_WRITES token 'bar' ignored" in messages


@pytest.mark.asyncio
async def test_decorator_blocks_when_env_unset() -> None:
    """Write decorator returns a structured block when env is unset."""

    @require_writes_enabled("marketing")
    async def delete_x() -> dict[str, bool]:
        return {"ok": True}

    result = await delete_x()

    assert result["error"] == "writes_disabled"
    assert result["tool"] == "delete_x"
    assert result["api"] == "marketing"


@pytest.mark.asyncio
async def test_decorator_allows_when_env_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write decorator calls through when all writes are enabled."""
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "all")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_WRITES_ALLOWED", "all")

    @require_writes_enabled("marketing")
    async def delete_x() -> dict[str, bool]:
        return {"ok": True}

    assert await delete_x() == {"ok": True}


@pytest.mark.asyncio
async def test_decorator_per_api_granularity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write decorator allows matching APIs and blocks absent APIs."""
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "marketing,comments")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_WRITES_ALLOWED", "marketing,comments")

    @require_writes_enabled("marketing")
    async def update_campaign() -> dict[str, bool]:
        return {"ok": True}

    @require_writes_enabled("posting")
    async def upload_video() -> dict[str, bool]:
        return {"ok": True}

    assert await update_campaign() == {"ok": True}
    blocked_result = await upload_video()
    assert blocked_result["error"] == "writes_disabled"
    assert blocked_result["api"] == "posting"


@pytest.mark.asyncio
async def test_decorator_env_toggle_mid_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write decorator rereads env each call so toggles take effect."""

    @require_writes_enabled("marketing")
    async def delete_x() -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setenv("TIKTOK_MCP_ALLOW_WRITES", "all")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_WRITES_ALLOWED", "all")
    assert await delete_x() == {"ok": True}

    monkeypatch.delenv("TIKTOK_MCP_ALLOW_WRITES", raising=False)
    blocked_result = await delete_x()
    assert blocked_result["error"] == "writes_disabled"


@pytest.mark.asyncio
async def test_structured_error_has_all_required_fields() -> None:
    """Blocked write responses contain exactly the required envelope keys."""

    @require_writes_enabled("marketing")
    async def delete_x() -> dict[str, bool]:
        return {"ok": True}

    result = await delete_x()

    assert set(result) == {"error", "message", "tool", "api", "would_have_done"}
    assert result["error"] == "writes_disabled"


def test_destructive_marker_attribute_set() -> None:
    """Write decorator sets destructive and write API marker attributes."""

    @require_writes_enabled("marketing")
    async def delete_x() -> dict[str, bool]:
        return {"ok": True}

    decorated_delete_x = cast(_DecoratedWriteTool, delete_x)
    assert is_destructive(delete_x)
    assert decorated_delete_x.__tiktok_mcp_destructive__ is True
    assert decorated_delete_x.__tiktok_mcp_write_api__ == "marketing"


@pytest.mark.asyncio
async def test_account_changes_decorator_blocks_when_env_unset() -> None:
    """Account-change decorator returns a structured block when env is unset."""

    @require_account_changes_enabled
    async def add_account() -> dict[str, bool]:
        return {"ok": True}

    result = await add_account()

    assert result["error"] == "account_changes_disabled"
    assert result["tool"] == "add_account"
    assert result["api"] == "account_changes"


@pytest.mark.asyncio
async def test_account_changes_decorator_allows_when_env_truthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Account-change decorator calls through when its binary env is truthy."""
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "yes")
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_CHANGES_ALLOWED", "yes")

    @require_account_changes_enabled
    async def add_account() -> dict[str, bool]:
        return {"ok": True}

    assert await add_account() == {"ok": True}


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("all", True),
    ),
)
def test_account_changes_env_truth_table(value: str | None, expected: bool) -> None:
    """Account-change env parsing uses binary truthy and falsy semantics."""
    assert parse_account_changes_env(value) is expected


def test_account_changes_enabled_reads_env_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Account-change helper rereads env each call so toggles take effect."""
    monkeypatch.setenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", "true")
    assert account_changes_enabled() is True

    monkeypatch.delenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", raising=False)
    assert account_changes_enabled() is False


def test_writes_enabled_for_validates_api_name() -> None:
    """Write helper rejects invalid API namespaces as compile-time bugs."""
    with pytest.raises(ValueError):
        _ = writes_enabled_for("invalid", env_value="all")


def test_assert_tool_decoration_compliance_catches_missing_decorator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compliance helper reports undecorated destructive-named functions."""
    module_name = "decorator_fixture_bad"
    fixture_module = tmp_path / f"{module_name}.py"
    _ = fixture_module.write_text("def create_bad():\n    pass\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    _ = sys.modules.pop(module_name, None)
    importlib.invalidate_caches()

    violations = assert_tool_decoration_compliance(module_name)

    assert any(
        "create_bad" in violation and "destructiveHint" in violation
        for violation in violations
    )


def test_assert_tool_decoration_compliance_accepts_marked_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compliance helper accepts marked write, account-change, and read tools."""
    module_name = "decorator_fixture_good"
    fixture_module = tmp_path / f"{module_name}.py"
    fixture_source = "\n".join(
        (
            "from tiktok_mcp.decorators import (",
            "    mark_read_only,",
            "    require_account_changes_enabled,",
            "    require_writes_enabled,",
            ")",
            "",
            "@require_writes_enabled('marketing')",
            "async def create_good():",
            "    return {'ok': True}",
            "",
            "@require_account_changes_enabled",
            "async def add_account():",
            "    return {'ok': True}",
            "",
            "@mark_read_only",
            "def get_good():",
            "    return {'ok': True}",
        )
    )
    _ = fixture_module.write_text(f"{fixture_source}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    _ = sys.modules.pop(module_name, None)
    importlib.invalidate_caches()

    assert assert_tool_decoration_compliance(module_name) == []


@pytest.mark.asyncio
async def test_structured_error_uses_summary_attribute() -> None:
    """Blocked write responses prefer the tool summary marker when present."""

    async def delete_x() -> dict[str, bool]:
        return {"ok": True}

    cast(_SummaryTool, delete_x).__tiktok_mcp_summary__ = "would delete x"
    decorated_delete_x = require_writes_enabled("marketing")(delete_x)

    result = await decorated_delete_x()

    assert result["would_have_done"] == "would delete x"


@pytest.mark.asyncio
async def test_display_read_tool_still_works_when_live_safety_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Display read tools are unaffected by the destructive live-account lock."""
    monkeypatch.setenv("TIKTOK_MCP_LIVE_ACCOUNT_SAFETY", "display")
    fake_client = _FakeDisplayClient()

    async def build_display_client(
        alias: str,
        *,
        sandbox: bool | None = None,
    ) -> tuple[Account, _FakeDisplayClient]:
        assert alias == "display-alias"
        assert sandbox is None
        return (
            Account(
                alias="display-alias",
                api_type=ApiType.DISPLAY,
                sandbox=False,
                tiktok_id="display-user",
                scopes=["user.info.basic"],
                status=AccountStatus.OK,
            ),
            fake_client,
        )

    async def request_json_object(
        client: _FakeDisplayClient,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        assert client is fake_client
        assert method == "GET"
        assert path == display_read_tools.USER_INFO_PATH
        assert params == {"fields": "open_id"}
        assert json_body is None
        return {"user": {"open_id": "open-basic", "display_name": "Basic Creator"}}

    monkeypatch.setattr(display_read_tools, "_build_display_client", build_display_client)
    monkeypatch.setattr(display_read_tools, "_request_json_object", request_json_object)

    result = await display_read_tools.display_get_user_info("display-alias", fields=["open_id"])

    assert result["open_id"] == "open-basic"
    assert fake_client.closed is True
