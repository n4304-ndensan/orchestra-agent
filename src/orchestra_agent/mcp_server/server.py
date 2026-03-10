from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestra_agent.mcp_server.excel_service import ExcelWorkspaceService
from orchestra_agent.mcp_server.file_service import WorkspaceFileService
from orchestra_agent.mcp_server.jsonrpc_server import ToolGroup, run_jsonrpc_mcp_server


def create_mcp_server(
    workspace_root: Path | str,
    server_name: str = "orchestra-workspace",
    tool_group: ToolGroup = "all",
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

    if tool_group in ("all", "files"):
        _register_file_tools(mcp, file_service)
    else:
        _register_server_ping(mcp)

    if tool_group in ("all", "excel"):
        _register_excel_tools(mcp, excel_service)

    return mcp


def run_mcp_server(
    workspace_root: Path | str,
    server_name: str = "orchestra-workspace",
    tool_group: ToolGroup = "all",
) -> None:
    server = create_mcp_server(
        workspace_root=workspace_root,
        server_name=server_name,
        tool_group=tool_group,
    )
    server.run()


def run_jsonrpc_server(
    workspace_root: Path | str,
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
    tool_group: ToolGroup = "all",
) -> None:
    run_jsonrpc_mcp_server(
        workspace_root=workspace_root,
        host=host,
        port=port,
        rpc_path=path,
        tool_group=tool_group,
    )


def _register_file_tools(mcp: Any, file_service: WorkspaceFileService) -> None:
    _register_server_ping(mcp)

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
    def fs_find_entries(
        pattern: str,
        path: str = ".",
        case_sensitive: bool = False,
        regex: bool = False,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Search file and directory names under the workspace."""
        return file_service.find_entries(
            pattern=pattern,
            path=path,
            case_sensitive=case_sensitive,
            regex=regex,
            include_dirs=include_dirs,
            max_results=max_results,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def fs_grep_text(
        pattern: str,
        path: str = ".",
        case_sensitive: bool = False,
        regex: bool = False,
        file_glob: str | None = None,
        max_results: int = 200,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Search text content recursively and return line matches."""
        return file_service.grep_text(
            pattern=pattern,
            path=path,
            case_sensitive=case_sensitive,
            regex=regex,
            file_glob=file_glob,
            max_results=max_results,
            encoding=encoding,
        )

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

    @mcp.tool()  # type: ignore[untyped-decorator]
    def fs_copy_file(
        source: str,
        destination: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Copy a file within the workspace."""
        copied = file_service.copy_file(source, destination, overwrite=overwrite)
        return {"copied": copied}


def _register_server_ping(mcp: Any) -> None:
    @mcp.tool()  # type: ignore[untyped-decorator]
    def server_ping() -> dict[str, str]:
        """Health check for the MCP server."""
        return {"status": "ok"}


def _register_excel_tools(mcp: Any, excel_service: ExcelWorkspaceService) -> None:
    _register_excel_read_tools(mcp, excel_service)
    _register_excel_write_tools(mcp, excel_service)
    _register_excel_media_tools(mcp, excel_service)


def _register_excel_read_tools(mcp: Any, excel_service: ExcelWorkspaceService) -> None:
    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_open_file(file: str) -> dict[str, Any]:
        """Open an Excel workbook and return sheet metadata."""
        return excel_service.open_file(file)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_read_sheet(file: str, sheet: str) -> dict[str, Any]:
        """Read worksheet rows as dictionaries keyed by column letters."""
        return excel_service.read_sheet(file, sheet)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_read_cells(file: str, sheet: str, cells: list[str]) -> dict[str, Any]:
        """Read specific worksheet cells."""
        return excel_service.read_cells(file, sheet, cells)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_grep_cells(
        file: str,
        pattern: str,
        sheet: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        exact: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        """Search workbook cell values like grep."""
        return excel_service.grep_cells(
            file,
            pattern,
            sheet=sheet,
            case_sensitive=case_sensitive,
            regex=regex,
            exact=exact,
            max_results=max_results,
        )

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


def _register_excel_write_tools(mcp: Any, excel_service: ExcelWorkspaceService) -> None:
    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_create_file(
        file: str,
        sheet: str = "Sheet1",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Create a new Excel workbook file under the workspace."""
        return excel_service.create_file(file, sheet=sheet, overwrite=overwrite)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_create_sheet(file: str, sheet: str, overwrite: bool = False) -> dict[str, Any]:
        """Create a worksheet inside an existing workbook."""
        return excel_service.create_sheet(file, sheet, overwrite)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_write_cells(file: str, sheet: str, cells: dict[str, Any]) -> dict[str, Any]:
        """Write cell values into a worksheet."""
        return excel_service.write_cells(file, sheet, cells)


def _register_excel_media_tools(mcp: Any, excel_service: ExcelWorkspaceService) -> None:
    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_list_images(file: str, sheet: str | None = None) -> dict[str, Any]:
        """List embedded worksheet images and their anchor cells."""
        return excel_service.list_images(file, sheet=sheet)

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_extract_image(
        file: str,
        sheet: str,
        image_index: int,
        output: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Extract a specific embedded image into the workspace."""
        return excel_service.extract_image(
            file,
            sheet=sheet,
            image_index=image_index,
            output=output,
            overwrite=overwrite,
        )

    @mcp.tool()  # type: ignore[untyped-decorator]
    def excel_save_file(file: str, output: str, overwrite: bool = True) -> dict[str, Any]:
        """Save or export a workbook into an output path."""
        return excel_service.save_file(file, output, overwrite)
