"""TikTok MCP server: Display, Marketing, Business Organic, Content Posting APIs."""

from __future__ import annotations

try:
    from tiktok_mcp._version import __version__
except ImportError:  # editable install before hatch-vcs has run
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
