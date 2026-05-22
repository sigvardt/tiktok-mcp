# pyright: reportImportCycles=false, reportMissingTypeStubs=false
# pyright: reportUnusedImport=false, reportUnusedCallResult=false
# pyright: reportAttributeAccessIssue=false, reportUnknownVariableType=false
"""Stdio MCP server entry point for tiktok-mcp.

Wave 1 T2 produces this as a minimal skeleton. Subsequent Wave 1+ tasks
populate tools, resources, and prompts before app.run() returns control.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from tiktok_mcp import __version__

app: FastMCP = FastMCP("tiktok-mcp")
from tiktok_mcp.tools import accounts as _accounts  # noqa: E402,F401
from tiktok_mcp.tools import app_credentials as _app_credentials  # noqa: E402,F401
from tiktok_mcp.tools import comments_read as _comments_read  # noqa: E402,F401
from tiktok_mcp.tools import marketing_reports as _marketing_reports  # noqa: E402,F401
from tiktok_mcp.tools import display_read as _display_read  # noqa: E402,F401
from tiktok_mcp.tools import marketing_read as _marketing_read  # noqa: E402,F401
from tiktok_mcp.tools import marketing_writes_ads as _marketing_writes_ads  # noqa: E402,F401
from tiktok_mcp.tools import marketing_writes_adgroups as _marketing_writes_adgroups  # noqa: E402,F401
from tiktok_mcp.tools import posting_read as _posting_read  # noqa: E402,F401
from tiktok_mcp.tools import rate_limit as _rate_limit  # noqa: E402,F401
from tiktok_mcp.tools import marketing_writes_campaigns as _marketing_writes_campaigns  # noqa: E402,F401


def main() -> None:
    """Entry point invoked by `tiktok-mcp` console script."""
    if len(sys.argv) > 1 and sys.argv[1] in {"--version", "-V"}:
        sys.stdout.write(f"tiktok-mcp {__version__}\n")
        return
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
