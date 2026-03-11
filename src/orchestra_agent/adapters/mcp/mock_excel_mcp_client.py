from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestra_agent.ports.mcp_client import IMcpClient


class MockExcelMcpClient(IMcpClient):
    """
    Local mock client for quick CLI verification without external MCP servers.
    """

    def __init__(self, fail_tools: set[str] | None = None) -> None:
        self.fail_tools = fail_tools or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._last_total = 0

    def list_tools(self) -> list[str]:
        return [tool["name"] for tool in self.describe_tools()]

    def describe_tools(self) -> list[dict[str, str]]:
        return [
            {
                "name": "list_sources",
                "description": "List available Excel sources.",
            },
            {
                "name": "find_workbooks",
                "description": "Search workbooks within a configured source.",
            },
            {
                "name": "resolve_workbook",
                "description": "Resolve a workbook reference from a path or remote descriptor.",
            },
            {
                "name": "inspect_workbook",
                "description": "Inspect workbook metadata, sheets, and tables.",
            },
            {
                "name": "list_sheets",
                "description": "List sheets in a workbook.",
            },
            {
                "name": "read_range",
                "description": "Read a cell range from a workbook.",
            },
            {
                "name": "read_table",
                "description": "Read an Excel table from a workbook.",
            },
            {
                "name": "search_workbook_text",
                "description": "Search workbook cell text.",
            },
            {
                "name": "open_edit_session",
                "description": "Open a safe workbook edit session.",
            },
            {
                "name": "stage_update_cells",
                "description": "Stage a cell update inside an edit session.",
            },
            {
                "name": "stage_append_rows",
                "description": "Stage row appends inside an edit session.",
            },
            {
                "name": "stage_create_sheet",
                "description": "Stage creation of a new sheet.",
            },
            {
                "name": "preview_edit_session",
                "description": "Preview staged workbook changes.",
            },
            {
                "name": "validate_edit_session",
                "description": "Validate a staged edit session.",
            },
            {
                "name": "commit_edit_session",
                "description": "Commit a staged edit session after preview and validation.",
            },
            {
                "name": "cancel_edit_session",
                "description": "Cancel an active edit session.",
            },
            {
                "name": "list_backups",
                "description": "List workbook backups.",
            },
            {
                "name": "restore_backup",
                "description": "Restore a workbook backup.",
            },
            {
                "name": "excel.create_file",
                "description": "Create a new Excel workbook file.",
            },
            {
                "name": "excel.open_file",
                "description": "Open an Excel workbook and inspect its sheets.",
            },
            {
                "name": "excel.read_sheet",
                "description": "Read worksheet rows as dictionaries keyed by column letters.",
            },
            {
                "name": "excel.read_cells",
                "description": "Read specific worksheet cells.",
            },
            {
                "name": "excel.grep_cells",
                "description": "Search workbook cell values like grep.",
            },
            {
                "name": "excel.calculate_sum",
                "description": "Calculate a numeric sum for a worksheet column range.",
            },
            {
                "name": "excel.create_sheet",
                "description": "Create a worksheet inside a workbook.",
            },
            {
                "name": "excel.write_cells",
                "description": "Write values into worksheet cells.",
            },
            {
                "name": "excel.list_images",
                "description": "List embedded worksheet images and anchor cells.",
            },
            {
                "name": "excel.extract_image",
                "description": "Extract a specific embedded image to the workspace.",
            },
            {
                "name": "excel.save_file",
                "description": "Save or export a workbook to an output path.",
            },
        ]

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_ref, dict(input)))
        if tool_ref in self.fail_tools:
            raise RuntimeError(f"forced failure: {tool_ref}")

        handler = self._tool_handlers().get(tool_ref)
        if handler is None:
            raise KeyError(f"Unsupported mock tool '{tool_ref}'.")
        return handler(input)

    def _tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "list_sources": self._list_sources,
            "find_workbooks": self._find_workbooks,
            "resolve_workbook": self._resolve_workbook,
            "inspect_workbook": self._inspect_workbook,
            "list_sheets": self._list_sheets,
            "read_range": self._read_range,
            "read_table": self._read_table,
            "search_workbook_text": self._search_workbook_text,
            "open_edit_session": self._open_edit_session,
            "stage_update_cells": self._stage_update_cells,
            "stage_append_rows": self._stage_append_rows,
            "stage_create_sheet": self._stage_create_sheet,
            "preview_edit_session": self._preview_edit_session,
            "validate_edit_session": self._validate_edit_session,
            "commit_edit_session": self._commit_edit_session,
            "cancel_edit_session": self._cancel_edit_session,
            "list_backups": self._list_backups,
            "restore_backup": self._restore_backup,
            "excel.create_file": self._create_file,
            "excel.open_file": self._open_file,
            "excel.read_sheet": self._read_sheet,
            "excel.read_cells": self._read_cells,
            "excel.grep_cells": self._grep_cells,
            "excel.calculate_sum": self._calculate_sum,
            "excel.create_sheet": self._create_sheet,
            "excel.write_cells": self._write_cells,
            "excel.list_images": self._list_images,
            "excel.extract_image": self._extract_image,
            "excel.save_file": self._save_file,
        }

    @staticmethod
    def _list_sources(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "sources": [
                {
                    "source_id": "local_workspace",
                    "source_type": "local_workspace",
                    "display_name": "Local Workspace",
                    "enabled": True,
                    "read_only": False,
                }
            ]
        }

    @staticmethod
    def _find_workbooks(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "workbook_refs": [
                {
                    "source_id": "local_workspace",
                    "source_type": "local_workspace",
                    "path": "demo.xlsx",
                    "name": "demo.xlsx",
                    "size": 0,
                    "modified_at": "1970-01-01T00:00:00+00:00",
                }
            ]
        }

    @staticmethod
    def _resolve_workbook(input: dict[str, Any]) -> dict[str, Any]:
        path = str(input.get("path", "demo.xlsx"))
        return {
            "workbook_ref": {
                "source_id": "local_workspace",
                "source_type": "local_workspace",
                "path": path,
                "name": Path(path).name,
                "size": 0,
                "modified_at": "1970-01-01T00:00:00+00:00",
                "hash": "mock",
            }
        }

    @staticmethod
    def _inspect_workbook(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "filename": "demo.xlsx",
            "size": 0,
            "modified_at": "1970-01-01T00:00:00+00:00",
            "hash": "mock",
            "extension": ".xlsx",
            "sheets": [{"name": "Sheet1", "hidden": False}],
            "tables": [],
            "named_ranges": [],
        }

    @staticmethod
    def _list_sheets(_: dict[str, Any]) -> dict[str, Any]:
        return {"sheets": [{"name": "Sheet1", "hidden": False}]}

    @staticmethod
    def _read_range(input: dict[str, Any]) -> dict[str, Any]:
        address = str(input.get("range", "A1"))
        return {"values": [["demo"]], "address": address, "row_count": 1, "col_count": 1}

    @staticmethod
    def _read_table(_: dict[str, Any]) -> dict[str, Any]:
        return {"headers": [], "rows": [], "address": "A1:A1", "row_count": 0}

    @staticmethod
    def _search_workbook_text(_: dict[str, Any]) -> dict[str, Any]:
        return {"matches": [], "truncated": False}

    @staticmethod
    def _open_edit_session(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": "mock-session",
            "base_version": "mock",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

    @staticmethod
    def _stage_update_cells(_: dict[str, Any]) -> dict[str, Any]:
        return {"operation_id": "mock-update", "affected_range": "A1", "warnings": []}

    @staticmethod
    def _stage_append_rows(_: dict[str, Any]) -> dict[str, Any]:
        return {"operation_id": "mock-append", "affected_table_or_range": "A2:A2", "warnings": []}

    @staticmethod
    def _stage_create_sheet(_: dict[str, Any]) -> dict[str, Any]:
        return {"operation_id": "mock-create-sheet"}

    @staticmethod
    def _preview_edit_session(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "preview": {
                "changed_sheets": ["Sheet1"],
                "changed_ranges": ["A1"],
                "old_value_count": 0,
                "new_value_count": 1,
                "formula_count": 0,
                "row_append_count": 0,
                "potential_risk_flags": [],
                "textual_summary": "mock preview",
            }
        }

    @staticmethod
    def _validate_edit_session(_: dict[str, Any]) -> dict[str, Any]:
        return {"valid": True, "errors": [], "warnings": []}

    @staticmethod
    def _commit_edit_session(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "commit_id": "mock-commit",
            "final_version": "mock",
            "backup_ref": None,
            "changed_targets": [],
        }

    @staticmethod
    def _cancel_edit_session(_: dict[str, Any]) -> dict[str, Any]:
        return {"canceled": True}

    @staticmethod
    def _list_backups(_: dict[str, Any]) -> dict[str, Any]:
        return {"backups": []}

    @staticmethod
    def _restore_backup(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "restore_result": {
                "restored": True,
                "backup_ref": "mock",
                "target": "demo.xlsx",
            }
        }

    @staticmethod
    def _create_file(input: dict[str, Any]) -> dict[str, Any]:
        file_path = Path(str(input["file"]))
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("mock workbook placeholder", encoding="utf-8")
        sheet = str(input.get("sheet", "Sheet1"))
        return {
            "file": str(file_path),
            "sheet_names": [sheet],
            "created": True,
            "overwritten": False,
        }

    @staticmethod
    def _open_file(input: dict[str, Any]) -> dict[str, Any]:
        return {"opened": input["file"]}

    @staticmethod
    def _read_sheet(_: dict[str, Any]) -> dict[str, Any]:
        return {"rows": [{"C": 10}, {"C": 20}, {"C": 30}]}

    @staticmethod
    def _read_cells(input: dict[str, Any]) -> dict[str, Any]:
        cells = input.get("cells", [])
        if not isinstance(cells, list):
            return {"cells": {}}
        return {"cells": {str(cell): None for cell in cells}}

    @staticmethod
    def _grep_cells(_: dict[str, Any]) -> dict[str, Any]:
        return {"matches": []}

    def _calculate_sum(self, _: dict[str, Any]) -> dict[str, Any]:
        self._last_total = 60
        return {"total": self._last_total}

    @staticmethod
    def _create_sheet(input: dict[str, Any]) -> dict[str, Any]:
        return {"created": input["sheet"]}

    def _write_cells(self, input: dict[str, Any]) -> dict[str, Any]:
        cells = input.get("cells", {})
        if isinstance(cells, dict):
            b2 = cells.get("B2")
            if isinstance(b2, int) and b2 != self._last_total:
                raise RuntimeError("write_cells expected resolved total in B2")
        return {"written_cells": len(cells) if isinstance(cells, dict) else 0}

    @staticmethod
    def _list_images(_: dict[str, Any]) -> dict[str, Any]:
        return {"images": []}

    @staticmethod
    def _extract_image(_: dict[str, Any]) -> dict[str, Any]:
        return {"output": None}

    @staticmethod
    def _save_file(input: dict[str, Any]) -> dict[str, Any]:
        output = Path(str(input["output"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("summary", encoding="utf-8")
        return {"output": str(output)}

