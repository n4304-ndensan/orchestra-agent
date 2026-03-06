from __future__ import annotations

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
        return [
            "excel.open_file",
            "excel.read_sheet",
            "excel.calculate_sum",
            "excel.create_sheet",
            "excel.write_cells",
            "excel.save_file",
        ]

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_ref, dict(input)))
        if tool_ref in self.fail_tools:
            raise RuntimeError(f"forced failure: {tool_ref}")

        if tool_ref == "excel.open_file":
            file_name = input["file"]
            return {"opened": file_name}
        if tool_ref == "excel.read_sheet":
            return {"rows": [{"C": 10}, {"C": 20}, {"C": 30}]}
        if tool_ref == "excel.calculate_sum":
            self._last_total = 60
            return {"total": self._last_total}
        if tool_ref == "excel.create_sheet":
            return {"created": input["sheet"]}
        if tool_ref == "excel.write_cells":
            cells = input.get("cells", {})
            if isinstance(cells, dict):
                b2 = cells.get("B2")
                if isinstance(b2, int) and b2 != self._last_total:
                    raise RuntimeError("write_cells expected resolved total in B2")
            return {"written_cells": len(cells) if isinstance(cells, dict) else 0}
        if tool_ref == "excel.save_file":
            output = Path(str(input["output"]))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("summary", encoding="utf-8")
            return {"output": str(output)}

        raise KeyError(f"Unsupported mock tool '{tool_ref}'.")

