# pyright: reportExplicitAny=false, reportAny=false, reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false, reportUnusedCallResult=false
# pyright: reportImplicitStringConcatenation=false
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
READ_TIMEOUT_SECONDS = 10.0
BOOT_WARNING_MS = 1500.0
EXPECTED_TOOL_COUNT_MIN = 71
EXPECTED_RESOURCE_URIS = {
    "tiktok-mcp://accounts/",
    "tiktok-mcp://app-credentials/",
}
EXPECTED_PROMPT_NAMES = {
    "weekly_marketing_report",
    "comment_queue_triage",
    "weekly_engagement_summary",
}
DESTRUCTIVE_NAME_FRAGMENTS = (
    "create",
    "update",
    "delete",
    "hide",
    "pin",
    "unhide",
    "unpin",
    "reply",
    "publish",
    "cancel",
    "move_draft_to_publish",
    "upload_",
    "refresh_token",
    "revoke_token",
    "add_account",
    "remove_account",
    "rename_account",
    "set_app_credentials",
    "complete_account_login",
)
READ_ONLY_NAME_FRAGMENTS = (
    "list_",
    "get_",
    "query_",
    "verify_",
    "marketing_run_sync_report",
    "marketing_run_async_report",
    "marketing_poll_async_report",
    "marketing_download_async_report",
    "get_rate_limit_status",
    "list_pending_drafts",
)

JsonObject = dict[str, Any]


def test_stdio_server_boots_and_lists_complete_registered_surface() -> None:
    env = os.environ.copy()
    env.setdefault("TIKTOK_MCP_LOG_LEVEL", "WARNING")
    start = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tiktok_mcp"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
    )

    try:
        _send_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "tiktok-mcp-stdio-test", "version": "0.0.0"},
                },
            },
        )
        initialize_response = _recv_response(proc, READ_TIMEOUT_SECONDS, expected_id=1)
        boot_ms = (time.perf_counter() - start) * 1000
        if boot_ms > BOOT_WARNING_MS:
            sys.stderr.write(
                f"WARNING: tiktok-mcp stdio boot took {boot_ms:.1f}ms, "
                f"above {BOOT_WARNING_MS:.0f}ms informational threshold\n"
            )

        initialize_result = _result_object(initialize_response)
        protocol_version = initialize_result.get("protocolVersion")
        assert isinstance(protocol_version, str) and protocol_version
        server_info = initialize_result.get("serverInfo")
        assert isinstance(server_info, dict)
        server_name = server_info.get("name")
        assert isinstance(server_name, str) and "tiktok" in server_name.lower()

        _send_request(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send_request(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_response = _recv_response(proc, READ_TIMEOUT_SECONDS, expected_id=2)
        tools = _list_result(tools_response, "tools")
        _assert_tools_complete_and_well_formed(tools)

        _send_request(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        resources_response = _recv_response(proc, READ_TIMEOUT_SECONDS, expected_id=3)
        resources = _list_result(resources_response, "resources")
        assert {resource.get("uri") for resource in resources} == EXPECTED_RESOURCE_URIS
        assert len(resources) == len(EXPECTED_RESOURCE_URIS)

        _send_request(proc, {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
        prompts_response = _recv_response(proc, READ_TIMEOUT_SECONDS, expected_id=4)
        prompts = _list_result(prompts_response, "prompts")
        assert {prompt.get("name") for prompt in prompts} == EXPECTED_PROMPT_NAMES
        assert len(prompts) == len(EXPECTED_PROMPT_NAMES)
    finally:
        _terminate_process(proc)


def _send_request(proc: subprocess.Popen[str], frame_dict: JsonObject) -> None:
    assert proc.stdin is not None
    payload = json.dumps(frame_dict, separators=(",", ":"))
    proc.stdin.write(f"{payload}\n")
    proc.stdin.flush()


def _recv_response(
    proc: subprocess.Popen[str],
    timeout: float,
    *,
    expected_id: int | None = None,
) -> JsonObject:
    deadline = time.monotonic() + timeout
    while True:
        payload = _recv_message(proc, deadline)
        if expected_id is None or payload.get("id") == expected_id:
            return payload


def _recv_message(proc: subprocess.Popen[str], deadline: float) -> JsonObject:
    while True:
        line = _readline_with_deadline(proc, deadline)
        if line == "":
            raise AssertionError(f"MCP server closed stdout. stderr={_finished_stderr(proc)!r}")
        if not line.strip():
            continue
        if line.lower().startswith("content-length:"):
            return _read_content_length_message(proc, line, deadline)
        return _json_object(line)


def _read_content_length_message(
    proc: subprocess.Popen[str],
    first_header: str,
    deadline: float,
) -> JsonObject:
    content_length = _content_length(first_header)
    while True:
        header = _readline_with_deadline(proc, deadline)
        if header in {"\r\n", "\n", ""}:
            break
        if header.lower().startswith("content-length:"):
            content_length = _content_length(header)

    assert proc.stdout is not None
    body = proc.stdout.read(content_length)
    if len(body) != content_length:
        raise AssertionError(
            f"Expected {content_length} chars of MCP body, got {len(body)}. "
            f"stderr={_finished_stderr(proc)!r}"
        )
    return _json_object(body)


def _content_length(header: str) -> int:
    try:
        return int(header.split(":", 1)[1].strip())
    except (IndexError, ValueError) as exc:
        raise AssertionError(f"Malformed Content-Length header: {header!r}") from exc


def _readline_with_deadline(proc: subprocess.Popen[str], deadline: float) -> str:
    assert proc.stdout is not None
    result: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

    def read_line() -> None:
        try:
            result.put(proc.stdout.readline())
        except BaseException as exc:
            result.put(exc)

    threading.Thread(target=read_line, daemon=True).start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Timed out waiting for MCP response. stderr={_finished_stderr(proc)!r}")
    try:
        item = result.get(timeout=remaining)
    except queue.Empty as exc:
        raise TimeoutError(
            f"Timed out waiting for MCP response. stderr={_finished_stderr(proc)!r}"
        ) from exc
    if isinstance(item, BaseException):
        raise item
    return item


def _json_object(raw_payload: str) -> JsonObject:
    payload = json.loads(raw_payload)
    assert isinstance(payload, dict), payload
    return payload


def _result_object(response: JsonObject) -> JsonObject:
    assert "error" not in response, response
    result = response.get("result")
    assert isinstance(result, dict), response
    return result


def _list_result(response: JsonObject, key: str) -> list[JsonObject]:
    result = _result_object(response)
    values = result.get(key)
    assert isinstance(values, list), result
    for value in values:
        assert isinstance(value, dict), value
    return values


def _assert_tools_complete_and_well_formed(tools: list[JsonObject]) -> None:
    assert len(tools) >= 40
    assert len(tools) == EXPECTED_TOOL_COUNT_MIN

    names = [tool.get("name") for tool in tools]
    assert all(isinstance(name, str) and name for name in names)
    tool_names = [name for name in names if isinstance(name, str)]
    assert len(tool_names) == len(set(tool_names))

    for tool in tools:
        name = tool["name"]
        assert isinstance(name, str)
        annotations = tool.get("annotations")
        assert isinstance(annotations, dict), f"{name} missing annotations"
        read_only_hint = annotations.get("readOnlyHint") is True
        destructive_hint = annotations.get("destructiveHint") is True
        assert read_only_hint != destructive_hint, (name, annotations)

        if name == "get_publish_status":
            assert read_only_hint, f"{name} should advertise readOnlyHint=True"
            continue
        if any(fragment in name for fragment in DESTRUCTIVE_NAME_FRAGMENTS):
            assert destructive_hint, f"{name} should advertise destructiveHint=True"
        elif any(fragment in name for fragment in READ_ONLY_NAME_FRAGMENTS):
            assert read_only_hint, f"{name} should advertise readOnlyHint=True"


def _finished_stderr(proc: subprocess.Popen[str]) -> str:
    if proc.poll() is None or proc.stderr is None:
        return ""
    try:
        return proc.stderr.read()
    except (OSError, ValueError):
        return ""


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.stdin is not None:
        with suppress(BrokenPipeError, OSError, ValueError):
            proc.stdin.close()
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
