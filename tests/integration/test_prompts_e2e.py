from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_SCRIPT = "from tiktok_mcp.server import main; main()"
READ_TIMEOUT_SECONDS = 30.0

EXPECTED_PROMPT_NAMES = {
    "weekly_marketing_report",
    "comment_queue_triage",
    "weekly_engagement_summary",
}


def _send(proc: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    assert proc.stdin is not None
    data = json.dumps(payload).encode("utf-8") + b"\n"
    proc.stdin.write(data)
    proc.stdin.flush()


def _readline_with_deadline(
    proc: subprocess.Popen[bytes], deadline: float
) -> bytes:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP server response timed out")
        ready = selector.select(timeout=remaining)
        if not ready:
            raise TimeoutError("MCP server response timed out")
    finally:
        selector.close()
    return proc.stdout.readline()


def _read_response(proc: subprocess.Popen[bytes], request_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
    while True:
        line = _readline_with_deadline(proc, deadline)
        if not line:
            stderr = b""
            if proc.stderr is not None:
                try:
                    stderr = proc.stderr.read() or b""
                except (OSError, ValueError):
                    stderr = b""
            raise RuntimeError(
                f"MCP server closed stdout before responding to id={request_id}. "
                f"stderr={stderr.decode('utf-8', errors='replace')!r}"
            )
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("id") == request_id:
            return payload


@pytest.fixture
def mcp_server() -> Iterator[subprocess.Popen[bytes]]:
    env = os.environ.copy()
    env.setdefault("TIKTOK_MCP_LOG_LEVEL", "WARNING")
    proc = subprocess.Popen(
        [sys.executable, "-c", LAUNCH_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
        bufsize=0,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except BrokenPipeError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _initialize(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tiktok-mcp-test", "version": "0.0.0"},
            },
        },
    )
    response = _read_response(proc, 1)
    assert "result" in response, response
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return response


def test_prompts_list_returns_all_three_prompts(
    mcp_server: subprocess.Popen[bytes],
) -> None:
    _initialize(mcp_server)
    _send(mcp_server, {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
    response = _read_response(mcp_server, 2)
    assert "result" in response, response
    prompts = response["result"].get("prompts")
    assert isinstance(prompts, list)
    names = {entry.get("name") for entry in prompts if isinstance(entry, dict)}
    assert EXPECTED_PROMPT_NAMES.issubset(names), names


def test_prompts_get_weekly_marketing_report_renders_inputs(
    mcp_server: subprocess.Popen[bytes],
) -> None:
    _initialize(mcp_server)
    _send(
        mcp_server,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "prompts/get",
            "params": {
                "name": "weekly_marketing_report",
                "arguments": {
                    "advertiser_alias": "no-marketing-e2e",
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-07",
                },
            },
        },
    )
    response = _read_response(mcp_server, 3)
    assert "result" in response, response
    messages = response["result"].get("messages")
    assert isinstance(messages, list) and messages
    rendered = "\n".join(
        msg.get("content", {}).get("text", "")
        for msg in messages
        if isinstance(msg, dict)
    )
    assert "no-marketing-e2e" in rendered
    assert "2026-04-01" in rendered
    assert "2026-04-07" in rendered
    assert "marketing_run_async_report" in rendered
    assert "Safety reminder:" in rendered


def test_prompts_get_comment_triage_surfaces_writes_gate(
    mcp_server: subprocess.Popen[bytes],
) -> None:
    _initialize(mcp_server)
    _send(
        mcp_server,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "prompts/get",
            "params": {
                "name": "comment_queue_triage",
                "arguments": {
                    "account_alias": "no-business-e2e",
                    "video_id": "7300000000000000001",
                },
            },
        },
    )
    response = _read_response(mcp_server, 4)
    assert "result" in response, response
    messages = response["result"].get("messages")
    assert isinstance(messages, list) and messages
    rendered = "\n".join(
        msg.get("content", {}).get("text", "")
        for msg in messages
        if isinstance(msg, dict)
    )
    assert "TIKTOK_MCP_ALLOW_WRITES" in rendered
    assert "comments_list" in rendered
    assert "no-business-e2e" in rendered
