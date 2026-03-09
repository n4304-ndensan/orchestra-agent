from __future__ import annotations

import json
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from orchestra_agent.mcp_server.excel_service import ExcelWorkspaceService
from orchestra_agent.mcp_server.file_service import WorkspaceFileService

type ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


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
        try:
            return handler(arguments)
        except JsonRpcError:
            raise
        except FileNotFoundError as exc:
            raise JsonRpcError(code=-32004, message=str(exc)) from exc
        except PermissionError as exc:
            raise JsonRpcError(code=-32003, message=str(exc)) from exc
        except (IsADirectoryError, NotADirectoryError, KeyError, ValueError) as exc:
            raise JsonRpcError(code=-32002, message=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise JsonRpcError(code=-32000, message=str(exc)) from exc


class JsonRpcMcpHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        workspace_root: Path,
        rpc_path: str,
    ) -> None:
        self.workspace_root = workspace_root
        self.rpc_path = rpc_path
        self.registry = build_tool_registry(workspace_root)
        super().__init__(server_address, request_handler_class)


class JsonRpcMcpRequestHandler(BaseHTTPRequestHandler):
    server: JsonRpcMcpHttpServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "workspace_root": str(self.server.workspace_root),
                    "rpc_path": self.server.rpc_path,
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != self.server.rpc_path:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            payload = self._read_json_body()
            response = self._handle_rpc(payload)
            self._send_json(HTTPStatus.OK, response)
        except JsonRpcError as exc:
            request_id = None
            if "payload" in locals() and isinstance(payload, dict):
                request_id = payload.get("id")
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


def build_tool_registry(workspace_root: Path) -> ToolRegistry:
    file_service = WorkspaceFileService(workspace_root)
    excel_service = ExcelWorkspaceService(workspace_root)
    registry = ToolRegistry()

    registry.register(
        "server_ping",
        "Health check for the MCP server.",
        lambda _: {"status": "ok"},
    )
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
        "excel.save_file",
        "Save or export a workbook into an output path.",
        lambda args: excel_service.save_file(
            path=str(args["file"]),
            output=str(args["output"]),
            overwrite=bool(args.get("overwrite", True)),
        ),
    )
    return registry


def run_jsonrpc_mcp_server(
    workspace_root: Path | str,
    host: str = "127.0.0.1",
    port: int = 8000,
    rpc_path: str = "/mcp",
) -> None:
    normalized_path = rpc_path if rpc_path.startswith("/") else f"/{rpc_path}"
    workspace = Path(workspace_root).resolve()
    server = JsonRpcMcpHttpServer(
        (host, port),
        JsonRpcMcpRequestHandler,
        workspace_root=workspace,
        rpc_path=normalized_path,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _require_cells(raw_cells: Any) -> dict[str, Any]:
    if not isinstance(raw_cells, dict):
        raise ValueError("excel.write_cells requires object 'cells'.")
    return raw_cells
