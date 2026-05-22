# pyright: reportImportCycles=false, reportMissingTypeStubs=false
# pyright: reportUnusedCallResult=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
"""Stdio MCP server entry point for tiktok-mcp."""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

app: FastMCP = FastMCP("tiktok-mcp")
if __name__ == "__main__":
    sys.modules["tiktok_mcp.server"] = sys.modules[__name__]

_components_registered = False


def _register_components() -> None:
    global _components_registered
    if _components_registered:
        return

    from tiktok_mcp.prompts import templates as prompts_templates
    from tiktok_mcp.resources import accounts as resources_accounts
    from tiktok_mcp.tools import accounts as tools_accounts
    from tiktok_mcp.tools import (
        app_credentials,
        comments_read,
        comments_writes,
        display_read,
        marketing_read,
        marketing_reports,
        marketing_writes_adgroups,
        marketing_writes_ads,
        marketing_writes_audiences,
        marketing_writes_campaigns,
        marketing_writes_creatives,
        posting_read,
        posting_writes_drafts,
        posting_writes_pull_and_photo,
        posting_writes_video_upload,
        rate_limit,
    )

    _ = (
        prompts_templates,
        resources_accounts,
        tools_accounts,
        app_credentials,
        comments_read,
        comments_writes,
        display_read,
        marketing_read,
        marketing_reports,
        marketing_writes_adgroups,
        marketing_writes_ads,
        marketing_writes_audiences,
        marketing_writes_campaigns,
        marketing_writes_creatives,
        posting_read,
        posting_writes_drafts,
        posting_writes_pull_and_photo,
        posting_writes_video_upload,
        rate_limit,
    )
    _components_registered = True


def main() -> None:
    """Launch the TikTok MCP server over stdio."""
    if "--version" in sys.argv[1:]:
        from tiktok_mcp import __version__

        sys.stdout.write(f"tiktok-mcp {__version__}\n")
        return

    _register_components()
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
