from __future__ import annotations

import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

from orchestra_agent.mcp_server.excel_service import ExcelToolError, ExcelWorkspaceService
from orchestra_agent.mcp_server.file_service import WorkspaceFileService
from orchestra_agent.mcp_server.logging_utils import get_mcp_logger, log_event, log_exception

type ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
type ToolGroup = Literal["all", "files", "excel"]

logger = get_mcp_logger(__name__)


class JsonRpcError(RuntimeError):
    def __init__(
        self,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[str, ToolHandler]] = {}

    def register(self, name: str, description: str, handler: ToolHandler) -> None:
        self._tools[name] = (description, handler)
        log_event(
            logger,
            "mcp_tool_registered",
            level=logging.DEBUG,
            tool=name,
            description=description,
        )

    def list_tools(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": description}
            for name, (description, _) in sorted(self._tools.items())
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise JsonRpcError(code=-32601, message=f"Unknown tool '{name}'.")
        _, handler = tool
        log_event(logger, "mcp_tool_call_started", tool=name, arguments=arguments)
        try:
            result = handler(arguments)
            log_event(
                logger,
                "mcp_tool_call_succeeded",
                tool=name,
                arguments=arguments,
                result=result,
            )
            return result
        except JsonRpcError:
            raise
        except ExcelToolError as exc:
            log_exception(logger, "mcp_tool_call_failed", exc, tool=name, arguments=arguments)
            raise JsonRpcError(code=-32010, message=exc.message, data=exc.to_dict()) from exc
        except FileNotFoundError as exc:
            log_exception(logger, "mcp_tool_call_failed", exc, tool=name, arguments=arguments)
            raise JsonRpcError(code=-32004, message=str(exc)) from exc
        except PermissionError as exc:
            log_exception(logger, "mcp_tool_call_failed", exc, tool=name, arguments=arguments)
            raise JsonRpcError(code=-32003, message=str(exc)) from exc
        except (IsADirectoryError, NotADirectoryError, KeyError, ValueError) as exc:
            log_exception(logger, "mcp_tool_call_failed", exc, tool=name, arguments=arguments)
            raise JsonRpcError(code=-32002, message=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "mcp_tool_call_failed", exc, tool=name, arguments=arguments)
            raise JsonRpcError(code=-32000, message=str(exc)) from exc


class JsonRpcMcpHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        workspace_root: Path,
        rpc_path: str,
        tool_group: ToolGroup = "all",
    ) -> None:
        self.workspace_root = workspace_root
        self.rpc_path = rpc_path
        self.tool_group = tool_group
        self.registry = build_tool_registry(workspace_root, tool_group=tool_group)
        super().__init__(server_address, request_handler_class)


class JsonRpcMcpRequestHandler(BaseHTTPRequestHandler):
    server: JsonRpcMcpHttpServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            log_event(
                logger,
                "mcp_http_healthcheck",
                path=self.path,
                client=self.client_address[0],
                port=self.client_address[1],
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "workspace_root": str(self.server.workspace_root),
                    "rpc_path": self.server.rpc_path,
                    "tool_group": self.server.tool_group,
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != self.server.rpc_path:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            log_event(
                logger,
                "mcp_http_request_received",
                path=self.path,
                client=self.client_address[0],
                port=self.client_address[1],
                content_length=self.headers.get("Content-Length", "0"),
            )
            payload = self._read_json_body()
            response = self._handle_rpc(payload)
            log_event(
                logger,
                "mcp_http_request_completed",
                path=self.path,
                request_id=payload.get("id"),
                method=payload.get("method"),
                response=response,
            )
            self._send_json(HTTPStatus.OK, response)
        except JsonRpcError as exc:
            request_id = None
            if "payload" in locals() and isinstance(payload, dict):
                request_id = payload.get("id")
            log_exception(
                logger,
                "mcp_http_request_failed",
                exc,
                path=self.path,
                request_id=request_id,
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "data": exc.data,
                    },
                },
            )
        except json.JSONDecodeError as exc:
            log_exception(logger, "mcp_http_request_invalid_json", exc, path=self.path)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Invalid JSON: {exc}"},
                },
            )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise JsonRpcError(code=-32600, message="Request body is required.")
        raw_body = self.rfile.read(content_length)
        parsed = json.loads(raw_body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise JsonRpcError(code=-32600, message="JSON-RPC payload must be an object.")
        return parsed

    def _handle_rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("jsonrpc") != "2.0":
            raise JsonRpcError(code=-32600, message="jsonrpc must be '2.0'.")
        method = payload.get("method")
        if not isinstance(method, str) or not method.strip():
            raise JsonRpcError(code=-32600, message="method must be a non-empty string.")
        params = payload.get("params", {})
        if not isinstance(params, dict):
            raise JsonRpcError(code=-32602, message="params must be an object.")

        log_event(
            logger,
            "mcp_rpc_dispatch",
            level=logging.DEBUG,
            request_id=payload.get("id"),
            method=method,
            params=params,
        )
        result = self._dispatch(method, params)
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "result": result,
        }

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "tools/list":
            return {"tools": self.server.registry.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str):
                raise JsonRpcError(code=-32602, message="tools/call requires string 'name'.")
            if not isinstance(arguments, dict):
                raise JsonRpcError(
                    code=-32602,
                    message="tools/call requires object 'arguments'.",
                )
            return self.server.registry.call_tool(name, arguments)
        if method == "server/ping":
            return {"status": "ok"}
        raise JsonRpcError(code=-32601, message=f"Unknown method '{method}'.")

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def build_tool_registry(workspace_root: Path, tool_group: ToolGroup = "all") -> ToolRegistry:
    file_service = WorkspaceFileService(workspace_root)
    excel_service = ExcelWorkspaceService(workspace_root)
    registry = ToolRegistry()

    registry.register(
        "server_ping",
        "Health check for the MCP server.",
        lambda _: {"status": "ok"},
    )
    if tool_group in ("all", "files"):
        registry.register(
            "fs_list_entries",
            "List files and directories under the workspace root.",
            lambda args: {
                "workspace_root": str(file_service.workspace_root),
                "entries": file_service.list_entries(str(args.get("path", "."))),
            },
        )
        registry.register(
            "fs_read_text",
            "Read a text file from the workspace.",
            lambda args: {
                "path": str(args["path"]),
                "content": file_service.read_text(
                    str(args["path"]),
                    encoding=str(args.get("encoding", "utf-8")),
                ),
            },
        )
        registry.register(
            "fs_find_entries",
            "Search file and directory names under the workspace.",
            lambda args: file_service.find_entries(
                pattern=str(args["pattern"]),
                path=str(args.get("path", ".")),
                case_sensitive=bool(args.get("case_sensitive", False)),
                regex=bool(args.get("regex", False)),
                include_dirs=bool(args.get("include_dirs", False)),
                max_results=_as_int(args.get("max_results", 200), "max_results"),
            ),
        )
        registry.register(
            "fs_grep_text",
            "Search text content recursively and return line matches.",
            lambda args: file_service.grep_text(
                pattern=str(args["pattern"]),
                path=str(args.get("path", ".")),
                case_sensitive=bool(args.get("case_sensitive", False)),
                regex=bool(args.get("regex", False)),
                file_glob=_optional_str(args.get("file_glob")),
                max_results=_as_int(args.get("max_results", 200), "max_results"),
                encoding=str(args.get("encoding", "utf-8")),
            ),
        )
        registry.register(
            "fs_write_text",
            "Write a text file under the workspace.",
            lambda args: {
                "written": file_service.write_text(
                    str(args["path"]),
                    str(args["content"]),
                    overwrite=bool(args.get("overwrite", False)),
                    encoding=str(args.get("encoding", "utf-8")),
                )
            },
        )
        registry.register(
            "fs_copy_file",
            "Copy a file within the workspace.",
            lambda args: {
                "copied": file_service.copy_file(
                    source_path=str(args["source"]),
                    destination_path=str(args["destination"]),
                    overwrite=bool(args.get("overwrite", False)),
                )
            },
        )

    if tool_group in ("all", "excel"):
        _register_excel_safe_tools(registry, excel_service)
        _register_excel_compat_tools(registry, excel_service)
    return registry


def _register_excel_safe_tools(
    registry: ToolRegistry,
    excel_service: ExcelWorkspaceService,
) -> None:
    registry.register(
        "list_sources",
        "List available Excel sources.",
        lambda args: excel_service.list_sources(
            include_disabled=bool(args.get("include_disabled", False))
        ),
    )
    registry.register(
        "find_workbooks",
        "Search workbooks within a configured source.",
        lambda args: excel_service.find_workbooks(
            source_id=str(args["source_id"]),
            query=str(args.get("query", "")),
            path_prefix=_optional_str(args.get("path_prefix")),
            recursive=bool(args.get("recursive", True)),
            limit=_optional_int(args.get("limit"), "limit"),
            extension_filter=_optional_str_list(args.get("extension_filter"), "extension_filter"),
        ),
    )
    registry.register(
        "resolve_workbook",
        "Resolve a workbook reference from a path or remote descriptor.",
        lambda args: excel_service.resolve_workbook(
            source_id=str(args["source_id"]),
            path=_optional_str(args.get("path")),
            remote_ref=_optional_mapping(args.get("remote_ref"), "remote_ref"),
        ),
    )
    registry.register(
        "inspect_workbook",
        "Inspect workbook metadata, sheets, and tables.",
        lambda args: excel_service.inspect_workbook(
            _require_workbook_ref(args.get("workbook_ref")),
            include_sheet_stats=bool(args.get("include_sheet_stats", True)),
            include_tables=bool(args.get("include_tables", False)),
        ),
    )
    registry.register(
        "list_sheets",
        "List sheets in a workbook.",
        lambda args: excel_service.list_sheets(_require_workbook_ref(args.get("workbook_ref"))),
    )
    registry.register(
        "read_range",
        "Read a cell range from a workbook.",
        lambda args: excel_service.read_range(
            _require_workbook_ref(args.get("workbook_ref")),
            sheet=str(args["sheet"]),
            range=str(args["range"]),
            value_render_mode=_as_value_render_mode(args.get("value_render_mode", "raw")),
            max_cells=_optional_int(args.get("max_cells"), "max_cells"),
        ),
    )
    registry.register(
        "read_table",
        "Read an Excel table from a workbook.",
        lambda args: excel_service.read_table(
            _require_workbook_ref(args.get("workbook_ref")),
            table_name=str(args["table_name"]),
            sheet=_optional_str(args.get("sheet")),
            max_rows=_optional_int(args.get("max_rows"), "max_rows"),
        ),
    )
    registry.register(
        "search_workbook_text",
        "Search workbook cell text.",
        lambda args: excel_service.search_workbook_text(
            _require_workbook_ref(args.get("workbook_ref")),
            pattern=str(args["pattern"]),
            match_case=bool(args.get("match_case", False)),
            exact=bool(args.get("exact", False)),
            max_results=_optional_int(args.get("max_results"), "max_results"),
        ),
    )
    registry.register(
        "open_edit_session",
        "Open a safe workbook edit session.",
        lambda args: excel_service.open_edit_session(
            _require_workbook_ref(args.get("workbook_ref")),
            source_mode=_optional_str(args.get("source_mode")),
            read_only=bool(args.get("read_only", False)),
            backup_policy=_optional_mapping(args.get("backup_policy"), "backup_policy"),
        ),
    )
    registry.register(
        "stage_update_cells",
        "Stage a cell update inside an edit session.",
        lambda args: excel_service.stage_update_cells(
            session_id=str(args["session_id"]),
            sheet=str(args["sheet"]),
            start_cell=str(args["start_cell"]),
            values=_require_2d_array(args.get("values"), "values"),
            write_mode=str(args.get("write_mode", "overwrite")),
            expected_existing_values=_optional_2d_array(
                args.get("expected_existing_values"),
                "expected_existing_values",
            ),
            allow_formula=_optional_bool(args.get("allow_formula"), "allow_formula"),
        ),
    )
    registry.register(
        "stage_append_rows",
        "Stage row appends inside an edit session.",
        lambda args: excel_service.stage_append_rows(
            session_id=str(args["session_id"]),
            sheet=str(args["sheet"]),
            rows=_require_2d_array(args.get("rows"), "rows"),
            table_name=_optional_str(args.get("table_name")),
            anchor_range=_optional_str(args.get("anchor_range")),
            schema_policy=str(args.get("schema_policy", "strict")),
        ),
    )
    registry.register(
        "stage_create_sheet",
        "Stage creation of a new sheet.",
        lambda args: excel_service.stage_create_sheet(
            session_id=str(args["session_id"]),
            new_sheet_name=str(args["new_sheet_name"]),
            template_sheet=_optional_str(args.get("template_sheet")),
        ),
    )
    registry.register(
        "preview_edit_session",
        "Preview staged workbook changes.",
        lambda args: excel_service.preview_edit_session(
            session_id=str(args["session_id"]),
            detail_level=_as_preview_level(args.get("detail_level", "summary")),
        ),
    )
    registry.register(
        "validate_edit_session",
        "Validate a staged edit session.",
        lambda args: excel_service.validate_edit_session(session_id=str(args["session_id"])),
    )
    registry.register(
        "commit_edit_session",
        "Commit a staged edit session after preview and validation.",
        lambda args: excel_service.commit_edit_session(
            session_id=str(args["session_id"]),
            commit_message=_optional_str(args.get("commit_message")),
            require_previewed=bool(args.get("require_previewed", True)),
            require_validated=_optional_bool(args.get("require_validated"), "require_validated"),
        ),
    )
    registry.register(
        "cancel_edit_session",
        "Cancel an active edit session.",
        lambda args: excel_service.cancel_edit_session(session_id=str(args["session_id"])),
    )
    registry.register(
        "list_backups",
        "List workbook backups.",
        lambda args: excel_service.list_backups(
            source_id=str(args["source_id"]),
            target=_optional_workbook_ref_or_str(args.get("target")),
            limit=_as_int(args.get("limit", 50), "limit"),
        ),
    )
    registry.register(
        "restore_backup",
        "Restore a workbook backup.",
        lambda args: excel_service.restore_backup(
            backup_ref=_require_backup_ref(args.get("backup_ref")),
            target_override=_optional_str(args.get("target_override")),
        ),
    )


def _register_excel_compat_tools(
    registry: ToolRegistry,
    excel_service: ExcelWorkspaceService,
) -> None:
    registry.register(
        "excel.create_file",
        "Create a new Excel workbook file under the workspace.",
        lambda args: excel_service.create_file(
            path=str(args["file"]),
            sheet=str(args.get("sheet", "Sheet1")),
            overwrite=bool(args.get("overwrite", False)),
        ),
    )
    registry.register(
        "excel.open_file",
        "Open an Excel workbook and return its sheet metadata.",
        lambda args: excel_service.open_file(str(args["file"])),
    )
    registry.register(
        "excel.read_sheet",
        "Read worksheet rows as dictionaries keyed by Excel column letters.",
        lambda args: excel_service.read_sheet(
            path=str(args["file"]),
            sheet=str(args["sheet"]),
        ),
    )
    registry.register(
        "excel.read_cells",
        "Read specific worksheet cells.",
        lambda args: excel_service.read_cells(
            path=str(args["file"]),
            sheet=str(args["sheet"]),
            cells=_require_str_list(args.get("cells"), field_name="cells"),
        ),
    )
    registry.register(
        "excel.grep_cells",
        "Search workbook cell values like grep.",
        lambda args: excel_service.grep_cells(
            path=str(args["file"]),
            pattern=str(args["pattern"]),
            sheet=_optional_str(args.get("sheet")),
            case_sensitive=bool(args.get("case_sensitive", False)),
            regex=bool(args.get("regex", False)),
            exact=bool(args.get("exact", False)),
            max_results=_as_int(args.get("max_results", 100), "max_results"),
        ),
    )
    registry.register(
        "excel.calculate_sum",
        "Calculate a numeric sum for a worksheet column.",
        lambda args: excel_service.calculate_sum(
            path=str(args["file"]),
            sheet=str(args["sheet"]),
            column=str(args["column"]),
            start_row=int(args["start_row"]) if "start_row" in args else None,
            end_row=int(args["end_row"]) if "end_row" in args else None,
        ),
    )
    registry.register(
        "excel.create_sheet",
        "Create a worksheet inside an existing workbook.",
        lambda args: excel_service.create_sheet(
            path=str(args["file"]),
            sheet=str(args["sheet"]),
            overwrite=bool(args.get("overwrite", False)),
        ),
    )
    registry.register(
        "excel.write_cells",
        "Write cell values into a worksheet.",
        lambda args: excel_service.write_cells(
            path=str(args["file"]),
            sheet=str(args["sheet"]),
            cells=_require_cells(args.get("cells")),
        ),
    )
    registry.register(
        "excel.list_images",
        "List embedded worksheet images and their anchor cells.",
        lambda args: excel_service.list_images(
            path=str(args["file"]),
            sheet=_optional_str(args.get("sheet")),
        ),
    )
    registry.register(
        "excel.extract_image",
        "Extract a specific embedded image into the workspace.",
        lambda args: excel_service.extract_image(
            path=str(args["file"]),
            sheet=str(args["sheet"]),
            image_index=_as_int(args["image_index"], "image_index"),
            output=_optional_str(args.get("output")),
            overwrite=bool(args.get("overwrite", False)),
        ),
    )
    registry.register(
        "excel.save_file",
        "Save or export a workbook into an output path.",
        lambda args: excel_service.save_file(
            path=str(args["file"]),
            output=str(args["output"]),
            overwrite=bool(args.get("overwrite", True)),
        ),
    )


def run_jsonrpc_mcp_server(
    workspace_root: Path | str,
    host: str = "127.0.0.1",
    port: int = 8000,
    rpc_path: str = "/mcp",
    tool_group: ToolGroup = "all",
) -> None:
    normalized_path = rpc_path if rpc_path.startswith("/") else f"/{rpc_path}"
    workspace = Path(workspace_root).resolve()
    server = JsonRpcMcpHttpServer(
        (host, port),
        JsonRpcMcpRequestHandler,
        workspace_root=workspace,
        rpc_path=normalized_path,
        tool_group=tool_group,
    )
    log_event(
        logger,
        "mcp_server_started",
        host=host,
        port=server.server_port,
        rpc_path=normalized_path,
        tool_group=tool_group,
        workspace_root=workspace,
    )
    try:
        server.serve_forever()
    finally:
        log_event(
            logger,
            "mcp_server_stopped",
            host=host,
            port=server.server_port,
            rpc_path=normalized_path,
            tool_group=tool_group,
            workspace_root=workspace,
        )
        server.server_close()


def _require_cells(raw_cells: Any) -> dict[str, Any]:
    if not isinstance(raw_cells, dict):
        raise ValueError("excel.write_cells requires object 'cells'.")
    return raw_cells


def _require_str_list(raw_value: Any, *, field_name: str) -> list[str]:
    if not isinstance(raw_value, list) or not all(isinstance(item, str) for item in raw_value):
        raise ValueError(f"{field_name} requires an array of strings.")
    return raw_value


def _optional_str(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError("Expected a string or null.")
    return raw_value


def _as_int(raw_value: Any, field_name: str) -> int:
    if not isinstance(raw_value, int) or isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    return raw_value


def _optional_int(raw_value: Any, field_name: str) -> int | None:
    if raw_value is None:
        return None
    return _as_int(raw_value, field_name)


def _optional_bool(raw_value: Any, field_name: str) -> bool | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be a boolean.")
    return raw_value


def _optional_mapping(raw_value: Any, field_name: str) -> dict[str, Any] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"{field_name} must be an object.")
    return raw_value


def _optional_str_list(raw_value: Any, field_name: str) -> list[str] | None:
    if raw_value is None:
        return None
    return _require_str_list(raw_value, field_name=field_name)


def _require_workbook_ref(raw_value: Any) -> dict[str, Any] | str:
    if isinstance(raw_value, str):
        return raw_value
    if not isinstance(raw_value, dict):
        raise ValueError("workbook_ref must be a path string or object.")
    return raw_value


def _optional_workbook_ref_or_str(raw_value: Any) -> dict[str, Any] | str | None:
    if raw_value is None:
        return None
    return _require_workbook_ref(raw_value)


def _require_backup_ref(raw_value: Any) -> dict[str, Any] | str:
    if isinstance(raw_value, str):
        return raw_value
    if not isinstance(raw_value, dict):
        raise ValueError("backup_ref must be a string or object.")
    return raw_value


def _require_2d_array(raw_value: Any, field_name: str) -> list[list[Any]]:
    if not isinstance(raw_value, list):
        raise ValueError(f"{field_name} must be a 2D array.")
    rows: list[list[Any]] = []
    for row in raw_value:
        if not isinstance(row, list):
            raise ValueError(f"{field_name} must be a 2D array.")
        rows.append(row)
    return rows


def _optional_2d_array(raw_value: Any, field_name: str) -> list[list[Any]] | None:
    if raw_value is None:
        return None
    return _require_2d_array(raw_value, field_name)


def _as_preview_level(raw_value: Any) -> str:
    if raw_value not in {"summary", "detailed", "cell_level"}:
        raise ValueError("detail_level must be summary, detailed, or cell_level.")
    return str(raw_value)


def _as_value_render_mode(raw_value: Any) -> str:
    if raw_value not in {"raw", "formatted", "formula"}:
        raise ValueError("value_render_mode must be raw, formatted, or formula.")
    return str(raw_value)
