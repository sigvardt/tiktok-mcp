"""Stdio MCP server entry point for tiktok-mcp.

Wave 1 T2 produces this as a minimal skeleton. Subsequent Wave 1+ tasks
populate tools, resources, and prompts before app.run() returns control.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from tiktok_mcp import __version__

app: FastMCP = FastMCP("tiktok-mcp")
from tiktok_mcp.tools import app_credentials as _app_credentials  # noqa: E402,F401  (register tools)



def main() -> None:
    """Entry point invoked by `tiktok-mcp` console script."""
    if len(sys.argv) > 1 and sys.argv[1] in {"--version", "-V"}:
        sys.stdout.write(f"tiktok-mcp {__version__}\n")
        return
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
