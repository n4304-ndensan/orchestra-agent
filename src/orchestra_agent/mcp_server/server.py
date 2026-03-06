from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestra_agent.mcp_server.file_service import WorkspaceFileService


def create_mcp_server(
    workspace_root: Path | str,
    server_name: str = "orchestra-workspace",
) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "Missing dependency 'mcp'. Install optional extras with "
            "`pip install \"orchestra-agent[mcp-server]\"`."
        ) from exc

    file_service = WorkspaceFileService(Path(workspace_root))
    mcp = FastMCP(server_name)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def server_ping() -> dict[str, str]:
        """Health check for the MCP server."""
        return {"status": "ok"}

    @mcp.tool()  # type: ignore[untyped-decorator]
    def fs_list_entries(path: str = ".") -> dict[str, Any]:
        """List files and directories under the workspace root."""
        return {
            "workspace_root": str(file_service.workspace_root),
            "entries": file_service.list_entries(path),
        }

    @mcp.tool()  # type: ignore[untyped-decorator]
    def fs_read_text(path: str, encoding: str = "utf-8") -> dict[str, Any]:
        """Read a text file from the workspace."""
        return {"path": path, "content": file_service.read_text(path, encoding=encoding)}

    @mcp.tool()  # type: ignore[untyped-decorator]
    def fs_write_text(
        path: str,
        content: str,
        overwrite: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Write a text file under the workspace."""
        result = file_service.write_text(path, content, overwrite=overwrite, encoding=encoding)
        return {"written": result}

    return mcp


def run_mcp_server(workspace_root: Path | str, server_name: str = "orchestra-workspace") -> None:
    server = create_mcp_server(workspace_root=workspace_root, server_name=server_name)
    server.run()
