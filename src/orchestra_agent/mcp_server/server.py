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
            '`pip install "orchestra-agent[mcp-server]"`.'
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
    _register_file_safe_tools(mcp, file_service)
    _register_file_compat_tools(mcp, file_service)


def _register_file_safe_tools(  # noqa: C901
    mcp: Any,
    file_service: WorkspaceFileService,
) -> None:
    @mcp.tool(name="file.list_sources")  # type: ignore[untyped-decorator]
    def file_list_sources(include_disabled: bool = False) -> dict[str, Any]:
        """List available file sources."""
        return file_service.list_sources(include_disabled=include_disabled)

    @mcp.tool(name="file.find_items")  # type: ignore[untyped-decorator]
    def file_find_items(
        source_id: str,
        query: str = "",
        parent: dict[str, Any] | str | None = None,
        recursive: bool = True,
        item_types: list[str] | None = None,
        extension_filter: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Search files and folders within a configured source."""
        return file_service.find_items(
            source_id=source_id,
            query=query,
            parent=parent,
            recursive=recursive,
            item_types=item_types,
            extension_filter=extension_filter,
            limit=limit,
        )

    @mcp.tool(name="file.resolve_item")  # type: ignore[untyped-decorator]
    def file_resolve_item(
        source_id: str,
        path: str | None = None,
        alias: str | None = None,
        remote_ref: dict[str, Any] | None = None,
        expected_type: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        """Resolve a file or folder reference from a path, alias, or remote descriptor."""
        return file_service.resolve_item(
            source_id=source_id,
            path=path,
            alias=alias,
            remote_ref=remote_ref,
            expected_type=expected_type,
            allow_missing=allow_missing,
        )

    @mcp.tool(name="file.list_children")  # type: ignore[untyped-decorator]
    def file_list_children(
        folder_ref: dict[str, Any] | str,
        recursive: bool = False,
        limit: int | None = None,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        """List child items under a folder."""
        return file_service.list_children(
            folder_ref,
            recursive=recursive,
            limit=limit,
            include_hidden=include_hidden,
        )

    @mcp.tool(name="file.get_item_metadata")  # type: ignore[untyped-decorator]
    def file_get_item_metadata(
        item_ref: dict[str, Any] | str,
        hashes: bool = False,
        permissions_summary: bool = False,
    ) -> dict[str, Any]:
        """Inspect file or folder metadata."""
        return file_service.get_item_metadata(
            item_ref,
            hashes=hashes,
            permissions_summary=permissions_summary,
        )

    @mcp.tool(name="file.read_text")  # type: ignore[untyped-decorator]
    def file_read_text(
        item_ref: dict[str, Any] | str,
        encoding: str | None = None,
        max_chars: int | None = None,
        normalize_newlines: bool = True,
    ) -> dict[str, Any]:
        """Read a text-like file."""
        return file_service.read_text_item(
            item_ref,
            encoding=encoding,
            max_chars=max_chars,
            normalize_newlines=normalize_newlines,
        )

    @mcp.tool(name="file.read_text_chunk")  # type: ignore[untyped-decorator]
    def file_read_text_chunk(
        item_ref: dict[str, Any] | str,
        offset: int,
        length: int,
        unit: str = "chars",
        encoding: str | None = None,
    ) -> dict[str, Any]:
        """Read a chunk from a text-like file."""
        return file_service.read_text_chunk(
            item_ref,
            offset=offset,
            length=length,
            unit=unit,
            encoding=encoding,
        )

    @mcp.tool(name="file.extract_document_text")  # type: ignore[untyped-decorator]
    def file_extract_document_text(
        item_ref: dict[str, Any] | str,
        max_chars: int | None = None,
        extraction_mode: str = "text_only",
    ) -> dict[str, Any]:
        """Extract text from a supported document-like file."""
        return file_service.extract_document_text(
            item_ref,
            max_chars=max_chars,
            extraction_mode=extraction_mode,
        )

    @mcp.tool(name="file.summarize_item")  # type: ignore[untyped-decorator]
    def file_summarize_item(
        item_ref: dict[str, Any] | str,
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        """Prepare a lightweight summary payload for a file."""
        return file_service.summarize_item(item_ref, max_chars=max_chars)

    @mcp.tool(name="file.open_text_edit_session")  # type: ignore[untyped-decorator]
    def file_open_text_edit_session(
        item_ref: dict[str, Any] | str,
        create_if_missing: bool = False,
        remote_mode: str | None = None,
    ) -> dict[str, Any]:
        """Open a safe text edit session."""
        return file_service.open_text_edit_session(
            item_ref,
            create_if_missing=create_if_missing,
            remote_mode=remote_mode,
        )

    @mcp.tool(name="file.stage_replace_text")  # type: ignore[untyped-decorator]
    def file_stage_replace_text(
        session_id: str,
        content: str,
        encoding: str | None = None,
        newline_mode: str = "preserve",
        expected_base_hash: str | None = None,
    ) -> dict[str, Any]:
        """Stage a full text replacement."""
        return file_service.stage_replace_text(
            session_id=session_id,
            content=content,
            encoding=encoding,
            newline_mode=newline_mode,
            expected_base_hash=expected_base_hash,
        )

    @mcp.tool(name="file.stage_patch_text")  # type: ignore[untyped-decorator]
    def file_stage_patch_text(
        session_id: str,
        patch_type: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Stage a patch-based text edit."""
        return file_service.stage_patch_text(
            session_id=session_id,
            patch_type=patch_type,
            operations=operations,
        )

    @mcp.tool(name="file.stage_insert_text")  # type: ignore[untyped-decorator]
    def file_stage_insert_text(
        session_id: str,
        position: str,
        content: str,
        byte_offset: int | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        """Stage a text insertion at a specific position."""
        return file_service.stage_insert_text(
            session_id=session_id,
            position=position,
            content=content,
            byte_offset=byte_offset,
            line_number=line_number,
        )

    @mcp.tool(name="file.stage_append_text")  # type: ignore[untyped-decorator]
    def file_stage_append_text(session_id: str, content: str) -> dict[str, Any]:
        """Stage a text append."""
        return file_service.stage_append_text(session_id=session_id, content=content)

    @mcp.tool(name="file.stage_create_text_file")  # type: ignore[untyped-decorator]
    def file_stage_create_text_file(
        parent_folder_ref: dict[str, Any] | str,
        file_name: str,
        encoding: str = "utf-8",
        content: str = "",
        if_exists: str = "fail",
    ) -> dict[str, Any]:
        """Stage creation of a new text file."""
        return file_service.stage_create_text_file(
            parent_folder_ref=parent_folder_ref,
            file_name=file_name,
            encoding=encoding,
            content=content,
            if_exists=if_exists,
        )

    @mcp.tool(name="file.stage_rename_item")  # type: ignore[untyped-decorator]
    def file_stage_rename_item(
        new_name: str,
        session_id: str | None = None,
        item_ref: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Stage a rename operation."""
        return file_service.stage_rename_item(
            new_name=new_name,
            session_id=session_id,
            item_ref=item_ref,
        )

    @mcp.tool(name="file.stage_move_item")  # type: ignore[untyped-decorator]
    def file_stage_move_item(
        destination_folder_ref: dict[str, Any] | str,
        conflict_policy: str = "fail",
        session_id: str | None = None,
        item_ref: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Stage a move operation."""
        return file_service.stage_move_item(
            destination_folder_ref=destination_folder_ref,
            conflict_policy=conflict_policy,
            session_id=session_id,
            item_ref=item_ref,
        )

    @mcp.tool(name="file.stage_copy_item")  # type: ignore[untyped-decorator]
    def file_stage_copy_item(
        destination_folder_ref: dict[str, Any] | str,
        new_name: str | None = None,
        overwrite: bool = False,
        session_id: str | None = None,
        item_ref: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Stage a copy operation."""
        return file_service.stage_copy_item(
            destination_folder_ref=destination_folder_ref,
            new_name=new_name,
            overwrite=overwrite,
            session_id=session_id,
            item_ref=item_ref,
        )

    @mcp.tool(name="file.stage_create_folder")  # type: ignore[untyped-decorator]
    def file_stage_create_folder(
        parent_folder_ref: dict[str, Any] | str,
        folder_name: str,
    ) -> dict[str, Any]:
        """Stage creation of a folder."""
        return file_service.stage_create_folder(
            parent_folder_ref=parent_folder_ref,
            folder_name=folder_name,
        )

    @mcp.tool(name="file.stage_delete_item")  # type: ignore[untyped-decorator]
    def file_stage_delete_item(
        deletion_mode: str = "soft_delete_preferred",
        session_id: str | None = None,
        item_ref: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Stage a delete operation when enabled by policy."""
        return file_service.stage_delete_item(
            deletion_mode=deletion_mode,
            session_id=session_id,
            item_ref=item_ref,
        )

    @mcp.tool(name="file.preview_file_edit_session")  # type: ignore[untyped-decorator]
    def file_preview_file_edit_session(session_id: str) -> dict[str, Any]:
        """Preview staged file changes."""
        return file_service.preview_file_edit_session(session_id=session_id)

    @mcp.tool(name="file.validate_file_edit_session")  # type: ignore[untyped-decorator]
    def file_validate_file_edit_session(session_id: str) -> dict[str, Any]:
        """Validate a staged file edit session."""
        return file_service.validate_file_edit_session(session_id=session_id)

    @mcp.tool(name="file.commit_file_edit_session")  # type: ignore[untyped-decorator]
    def file_commit_file_edit_session(
        session_id: str,
        commit_message: str | None = None,
        require_previewed: bool = True,
        require_validated: bool | None = None,
    ) -> dict[str, Any]:
        """Commit a staged file edit session after preview and validation."""
        return file_service.commit_file_edit_session(
            session_id=session_id,
            commit_message=commit_message,
            require_previewed=require_previewed,
            require_validated=require_validated,
        )

    @mcp.tool(name="file.cancel_file_edit_session")  # type: ignore[untyped-decorator]
    def file_cancel_file_edit_session(session_id: str) -> dict[str, Any]:
        """Cancel an active file edit session."""
        return file_service.cancel_file_edit_session(session_id=session_id)

    @mcp.tool(name="file.list_backups")  # type: ignore[untyped-decorator]
    def file_list_backups(
        source_id: str,
        target: dict[str, Any] | str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List file backups."""
        return file_service.list_backups(source_id=source_id, target=target, limit=limit)

    @mcp.tool(name="file.restore_backup")  # type: ignore[untyped-decorator]
    def file_restore_backup(
        backup_ref: dict[str, Any] | str,
        target_override: str | None = None,
    ) -> dict[str, Any]:
        """Restore a file backup."""
        return file_service.restore_backup(
            backup_ref=backup_ref,
            target_override=target_override,
        )


def _register_file_compat_tools(mcp: Any, file_service: WorkspaceFileService) -> None:
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
