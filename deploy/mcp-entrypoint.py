"""Container entrypoint, independent of the upstream Workspace MCP server."""
from campuspulse_recovery.mcp_service.server import main

if __name__ == "__main__":
    main()
