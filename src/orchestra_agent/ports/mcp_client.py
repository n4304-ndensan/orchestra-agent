from __future__ import annotations

from typing import Any, Protocol


class IMcpClient(Protocol):
    def list_tools(self) -> list[str]:
        ...

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        ...

