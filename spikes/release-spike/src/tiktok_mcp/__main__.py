import sys

from . import __version__


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(f"tiktok-mcp {__version__}")
        sys.exit(0)
    # Spike scaffolding: no MCP server in this throwaway. Real entrypoint is Wave 1 T1.
    print("tiktok-mcp S2 spike — see spikes/release-spike/README.md")
    sys.exit(0)


if __name__ == "__main__":
    main()
