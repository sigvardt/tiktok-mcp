# pyright: reportImportCycles=false
"""TikTok MCP server: Display, Marketing, Business Organic, Content Posting APIs."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _read_version() -> str:
    try:
        return version("tiktok-mcp")
    except PackageNotFoundError:
        try:
            from ._version import __version__ as generated_version
        except ImportError:
            return "0.0.0+unknown"
        return generated_version


__version__: str = _read_version()

from .server import app, main  # noqa: E402

__all__ = ["__version__", "app", "main"]
