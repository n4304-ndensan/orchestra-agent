from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestra_agent.mcp_server.excel_service import ExcelWorkspaceService
from orchestra_agent.mcp_server.file_service import WorkspaceFileService
from orchestra_agent.mcp_server.jsonrpc_server import ToolGroup, run_jsonrpc_mcp_server
from orchestra_agent.mcp_server.logging_utils import get_mcp_logger, log_event

logger = get_mcp_logger(__name__)


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
    log_event(
        logger,
        "mcp_stdio_server_starting",
        server_name=server_name,
        tool_group=tool_group,
        workspace_root=Path(workspace_root).resolve(),
    )
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
    _register_excel_safe_tools(mcp, excel_service)
    _register_excel_read_tools(mcp, excel_service)
    _register_excel_write_tools(mcp, excel_service)
    _register_excel_media_tools(mcp, excel_service)


def _register_excel_safe_tools(  # noqa: C901
    mcp: Any,
    excel_service: ExcelWorkspaceService,
) -> None:
    @mcp.tool(name="list_sources")  # type: ignore[untyped-decorator]
    def list_sources(include_disabled: bool = False) -> dict[str, Any]:
        """List available Excel sources."""
        return excel_service.list_sources(include_disabled=include_disabled)

    @mcp.tool(name="find_workbooks")  # type: ignore[untyped-decorator]
    def find_workbooks(
        source_id: str,
        query: str = "",
        path_prefix: str | None = None,
        recursive: bool = True,
        limit: int | None = None,
        extension_filter: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search workbooks within a configured source."""
        return excel_service.find_workbooks(
            source_id=source_id,
            query=query,
            path_prefix=path_prefix,
            recursive=recursive,
            limit=limit,
            extension_filter=extension_filter,
        )

    @mcp.tool(name="resolve_workbook")  # type: ignore[untyped-decorator]
    def resolve_workbook(
        source_id: str,
        path: str | None = None,
        remote_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve a workbook reference from a path or remote descriptor."""
        return excel_service.resolve_workbook(
            source_id=source_id,
            path=path,
            remote_ref=remote_ref,
        )

    @mcp.tool(name="inspect_workbook")  # type: ignore[untyped-decorator]
    def inspect_workbook(
        workbook_ref: dict[str, Any] | str,
        include_sheet_stats: bool = True,
        include_tables: bool = False,
    ) -> dict[str, Any]:
        """Inspect workbook metadata, sheets, and tables."""
        return excel_service.inspect_workbook(
            workbook_ref,
            include_sheet_stats=include_sheet_stats,
            include_tables=include_tables,
        )

    @mcp.tool(name="list_sheets")  # type: ignore[untyped-decorator]
    def list_sheets(workbook_ref: dict[str, Any] | str) -> dict[str, Any]:
        """List sheets in a workbook."""
        return excel_service.list_sheets(workbook_ref)

    @mcp.tool(name="read_range")  # type: ignore[untyped-decorator]
    def read_range(
        workbook_ref: dict[str, Any] | str,
        sheet: str,
        range: str,
        value_render_mode: str = "raw",
        max_cells: int | None = None,
    ) -> dict[str, Any]:
        """Read a cell range from a workbook."""
        return excel_service.read_range(
            workbook_ref,
            sheet=sheet,
            range=range,
            value_render_mode=value_render_mode,  # type: ignore[arg-type]
            max_cells=max_cells,
        )

    @mcp.tool(name="read_table")  # type: ignore[untyped-decorator]
    def read_table(
        workbook_ref: dict[str, Any] | str,
        table_name: str,
        sheet: str | None = None,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """Read an Excel table from a workbook."""
        return excel_service.read_table(
            workbook_ref,
            table_name=table_name,
            sheet=sheet,
            max_rows=max_rows,
        )

    @mcp.tool(name="search_workbook_text")  # type: ignore[untyped-decorator]
    def search_workbook_text(
        workbook_ref: dict[str, Any] | str,
        pattern: str,
        match_case: bool = False,
        exact: bool = False,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Search workbook cell text."""
        return excel_service.search_workbook_text(
            workbook_ref,
            pattern=pattern,
            match_case=match_case,
            exact=exact,
            max_results=max_results,
        )

    @mcp.tool(name="open_edit_session")  # type: ignore[untyped-decorator]
    def open_edit_session(
        workbook_ref: dict[str, Any] | str,
        source_mode: str | None = None,
        read_only: bool = False,
        backup_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open a safe workbook edit session."""
        return excel_service.open_edit_session(
            workbook_ref,
            source_mode=source_mode,
            read_only=read_only,
            backup_policy=backup_policy,
        )

    @mcp.tool(name="stage_update_cells")  # type: ignore[untyped-decorator]
    def stage_update_cells(
        session_id: str,
        sheet: str,
        start_cell: str,
        values: list[list[Any]],
        write_mode: str = "overwrite",
        expected_existing_values: list[list[Any]] | None = None,
        allow_formula: bool | None = None,
    ) -> dict[str, Any]:
        """Stage a cell update inside an edit session."""
        return excel_service.stage_update_cells(
            session_id=session_id,
            sheet=sheet,
            start_cell=start_cell,
            values=values,
            write_mode=write_mode,
            expected_existing_values=expected_existing_values,
            allow_formula=allow_formula,
        )

    @mcp.tool(name="stage_append_rows")  # type: ignore[untyped-decorator]
    def stage_append_rows(
        session_id: str,
        sheet: str,
        rows: list[list[Any]],
        table_name: str | None = None,
        anchor_range: str | None = None,
        schema_policy: str = "strict",
    ) -> dict[str, Any]:
        """Stage row appends inside an edit session."""
        return excel_service.stage_append_rows(
            session_id=session_id,
            sheet=sheet,
            rows=rows,
            table_name=table_name,
            anchor_range=anchor_range,
            schema_policy=schema_policy,
        )

    @mcp.tool(name="stage_create_sheet")  # type: ignore[untyped-decorator]
    def stage_create_sheet(
        session_id: str,
        new_sheet_name: str,
        template_sheet: str | None = None,
    ) -> dict[str, Any]:
        """Stage creation of a new sheet."""
        return excel_service.stage_create_sheet(
            session_id=session_id,
            new_sheet_name=new_sheet_name,
            template_sheet=template_sheet,
        )

    @mcp.tool(name="preview_edit_session")  # type: ignore[untyped-decorator]
    def preview_edit_session(
        session_id: str,
        detail_level: str = "summary",
    ) -> dict[str, Any]:
        """Preview staged workbook changes."""
        return excel_service.preview_edit_session(
            session_id=session_id,
            detail_level=detail_level,  # type: ignore[arg-type]
        )

    @mcp.tool(name="validate_edit_session")  # type: ignore[untyped-decorator]
    def validate_edit_session(session_id: str) -> dict[str, Any]:
        """Validate a staged edit session."""
        return excel_service.validate_edit_session(session_id=session_id)

    @mcp.tool(name="commit_edit_session")  # type: ignore[untyped-decorator]
    def commit_edit_session(
        session_id: str,
        commit_message: str | None = None,
        require_previewed: bool = True,
        require_validated: bool | None = None,
    ) -> dict[str, Any]:
        """Commit a staged edit session after preview and validation."""
        return excel_service.commit_edit_session(
            session_id=session_id,
            commit_message=commit_message,
            require_previewed=require_previewed,
            require_validated=require_validated,
        )

    @mcp.tool(name="cancel_edit_session")  # type: ignore[untyped-decorator]
    def cancel_edit_session(session_id: str) -> dict[str, Any]:
        """Cancel an active edit session."""
        return excel_service.cancel_edit_session(session_id=session_id)

    @mcp.tool(name="list_backups")  # type: ignore[untyped-decorator]
    def list_backups(
        source_id: str,
        target: dict[str, Any] | str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List workbook backups."""
        return excel_service.list_backups(
            source_id=source_id,
            target=target,
            limit=limit,
        )

    @mcp.tool(name="restore_backup")  # type: ignore[untyped-decorator]
    def restore_backup(
        backup_ref: dict[str, Any] | str,
        target_override: str | None = None,
    ) -> dict[str, Any]:
        """Restore a workbook backup."""
        return excel_service.restore_backup(
            backup_ref=backup_ref,
            target_override=target_override,
        )


def _register_excel_read_tools(mcp: Any, excel_service: ExcelWorkspaceService) -> None:
    @mcp.tool(name="excel.open_file")  # type: ignore[untyped-decorator]
    def excel_open_file(file: str) -> dict[str, Any]:
        """Open an Excel workbook and return sheet metadata."""
        return excel_service.open_file(file)

    @mcp.tool(name="excel.read_sheet")  # type: ignore[untyped-decorator]
    def excel_read_sheet(file: str, sheet: str) -> dict[str, Any]:
        """Read worksheet rows as dictionaries keyed by column letters."""
        return excel_service.read_sheet(file, sheet)

    @mcp.tool(name="excel.read_cells")  # type: ignore[untyped-decorator]
    def excel_read_cells(file: str, sheet: str, cells: list[str]) -> dict[str, Any]:
        """Read specific worksheet cells."""
        return excel_service.read_cells(file, sheet, cells)

    @mcp.tool(name="excel.grep_cells")  # type: ignore[untyped-decorator]
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

    @mcp.tool(name="excel.calculate_sum")  # type: ignore[untyped-decorator]
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
    @mcp.tool(name="excel.create_file")  # type: ignore[untyped-decorator]
    def excel_create_file(
        file: str,
        sheet: str = "Sheet1",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Create a new Excel workbook file under the workspace."""
        return excel_service.create_file(file, sheet=sheet, overwrite=overwrite)

    @mcp.tool(name="excel.create_sheet")  # type: ignore[untyped-decorator]
    def excel_create_sheet(file: str, sheet: str, overwrite: bool = False) -> dict[str, Any]:
        """Create a worksheet inside an existing workbook."""
        return excel_service.create_sheet(file, sheet, overwrite)

    @mcp.tool(name="excel.write_cells")  # type: ignore[untyped-decorator]
    def excel_write_cells(file: str, sheet: str, cells: dict[str, Any]) -> dict[str, Any]:
        """Write cell values into a worksheet."""
        return excel_service.write_cells(file, sheet, cells)


def _register_excel_media_tools(mcp: Any, excel_service: ExcelWorkspaceService) -> None:
    @mcp.tool(name="excel.list_images")  # type: ignore[untyped-decorator]
    def excel_list_images(file: str, sheet: str | None = None) -> dict[str, Any]:
        """List embedded worksheet images and their anchor cells."""
        return excel_service.list_images(file, sheet=sheet)

    @mcp.tool(name="excel.extract_image")  # type: ignore[untyped-decorator]
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

    @mcp.tool(name="excel.save_file")  # type: ignore[untyped-decorator]
    def excel_save_file(file: str, output: str, overwrite: bool = True) -> dict[str, Any]:
        """Save or export a workbook into an output path."""
        return excel_service.save_file(file, output, overwrite)
