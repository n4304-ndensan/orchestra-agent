from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestra_agent.mcp_server.excel_service import ExcelWorkspaceService
from orchestra_agent.mcp_server.file_service import WorkspaceFileService
from orchestra_agent.mcp_server.jsonrpc_server import run_jsonrpc_mcp_server


def create_mcp_server(
    workspace_root: Path | str,
    server_name: str = "orchestra-workspace",
) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "Missing dependency 'mcp'. Install optional extras with "
            "`pip install \"orchestra-agent[mcp-server]\"`."
        ) from exc

    workspace = Path(workspace_root)
    file_service = WorkspaceFileService(workspace)
    excel_service = ExcelWorkspaceService(workspace)
    mcp = FastMCP(server_name)

    _register_file_tools(mcp, file_service)
    _register_excel_tools(mcp, excel_service)

    return mcp


def run_mcp_server(workspace_root: Path | str, server_name: str = "orchestra-workspace") -> None:
    server = create_mcp_server(workspace_root=workspace_root, server_name=server_name)
    server.run()


def run_jsonrpc_server(
    workspace_root: Path | str,
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
) -> None:
    run_jsonrpc_mcp_server(workspace_root=workspace_root, host=host, port=port, rpc_path=path)


def _register_file_tools(mcp: Any, file_service: WorkspaceFileService) -> None:
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


def _register_excel_tools(mcp: Any, excel_service: ExcelWorkspaceService) -> None:
    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_open_file(file: str) -> dict[str, Any]:
        """Open an Excel workbook and return sheet metadata."""
        return excel_service.open_file(file)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_read_sheet(file: str, sheet: str) -> dict[str, Any]:
        """Read worksheet rows as dictionaries keyed by column letters."""
        return excel_service.read_sheet(file, sheet)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_calculate_sum(
        file: str,
        sheet: str,
        column: str,
        start_row: int | None = None,
        end_row: int | None = None,
    ) -> dict[str, Any]:
        """Calculate a numeric sum for a worksheet column."""
        return excel_service.calculate_sum(file, sheet, column, start_row, end_row)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_create_sheet(file: str, sheet: str, overwrite: bool = False) -> dict[str, Any]:
        """Create a worksheet inside an existing workbook."""
        return excel_service.create_sheet(file, sheet, overwrite)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_write_cells(file: str, sheet: str, cells: dict[str, Any]) -> dict[str, Any]:
        """Write cell values into a worksheet."""
        return excel_service.write_cells(file, sheet, cells)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_save_file(file: str, output: str, overwrite: bool = True) -> dict[str, Any]:
        """Save or export a workbook into an output path."""
        return excel_service.save_file(file, output, overwrite)
