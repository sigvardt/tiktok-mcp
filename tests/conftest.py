"""Shared pytest fixtures and configuration for tiktok-mcp test suite."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def clear_writes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure write gates are unset for every test by default."""
    monkeypatch.delenv("TIKTOK_MCP_ALLOW_WRITES", raising=False)
    monkeypatch.delenv("TIKTOK_MCP_ALLOW_LIVE_WRITES", raising=False)
    monkeypatch.delenv("TIKTOK_MCP_ALLOW_ACCOUNT_CHANGES", raising=False)


@pytest.fixture
def vcr_cassette_dir() -> str:
    """Default cassette directory for vcrpy-based tests."""
    return os.path.join(os.path.dirname(__file__), "cassettes")


@pytest.fixture
def vcr_config(vcr_cassette_dir: str) -> dict[str, str]:
    """Default vcrpy configuration for the local cassette directory."""
    return {"cassette_library_dir": vcr_cassette_dir}
