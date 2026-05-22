"""Smoke test: package imports and exposes a non-empty version string."""

from __future__ import annotations


def test_package_imports() -> None:
    """Importing tiktok_mcp must succeed and expose __version__ as a non-empty str."""
    import tiktok_mcp

    assert isinstance(tiktok_mcp.__version__, str)
    assert tiktok_mcp.__version__, "version must not be an empty string"
