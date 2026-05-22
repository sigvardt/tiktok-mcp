from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import IO, cast

from mcp.types import LATEST_PROTOCOL_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_resources_are_listed_by_the_mcp_server() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "tiktok_mcp.server"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    try:
        _send_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
        )
        initialize_response = _read_message(process.stdout, expected_id=1)
        initialize_result = cast(dict[str, object], initialize_response["result"])
        assert cast(str, initialize_result["protocolVersion"]) == LATEST_PROTOCOL_VERSION

        _send_message(
            process.stdin,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        _send_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/list",
                "params": {},
            },
        )
        resources_response = _read_message(process.stdout, expected_id=2)
        resources_result = cast(dict[str, object], resources_response["result"])
        resources = cast(list[dict[str, object]], resources_result["resources"])
        resource_by_uri = {
            cast(str, resource["uri"]): resource
            for resource in resources
        }

        assert set(resource_by_uri) == {
            "tiktok-mcp://accounts/",
            "tiktok-mcp://app-credentials/",
        }
        assert resource_by_uri["tiktok-mcp://accounts/"]["mimeType"] == "application/json"
        assert resource_by_uri["tiktok-mcp://app-credentials/"]["mimeType"] == "application/json"
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            _ = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.wait(timeout=5)


def _send_message(stream: IO[bytes], payload: dict[str, object]) -> None:
    encoded_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    _ = stream.write(encoded_payload + b"\n")
    stream.flush()


def _read_message(stream: IO[bytes], *, expected_id: int) -> dict[str, object]:
    while True:
        message = _read_single_message(stream)
        if message.get("id") == expected_id:
            return message


def _read_single_message(stream: IO[bytes]) -> dict[str, object]:
    line = stream.readline()
    if not line:
        raise AssertionError("MCP server closed stdout before responding.")
    return cast(dict[str, object], json.loads(line))
