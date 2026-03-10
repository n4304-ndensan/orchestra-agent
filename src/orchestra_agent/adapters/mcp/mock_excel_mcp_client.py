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

