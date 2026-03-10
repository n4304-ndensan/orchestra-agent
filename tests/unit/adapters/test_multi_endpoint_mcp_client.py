from __future__ import annotations

from typing import Any

import pytest

from orchestra_agent.adapters.mcp import MultiEndpointMcpClient


class StubMcpClient:
    def __init__(
        self,
        tools: list[str],
        result_by_tool: dict[str, dict[str, Any]],
        descriptions: dict[str, str] | None = None,
    ) -> None:
        self._tools = tools
        self._result_by_tool = result_by_tool
        self._descriptions = descriptions or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[str]:
        return list(self._tools)

    def describe_tools(self) -> list[dict[str, str]]:
        return [
            {
                "name": tool_name,
                "description": self._descriptions.get(tool_name, ""),
            }
            for tool_name in self._tools
        ]

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_ref, input))
        return self._result_by_tool[tool_ref]


def test_multi_endpoint_client_merges_and_routes_tools() -> None:
    files_client = StubMcpClient(
        tools=["fs_list_entries"],
        result_by_tool={"fs_list_entries": {"entries": []}},
    )
    excel_client = StubMcpClient(
        tools=["excel.open_file"],
        result_by_tool={"excel.open_file": {"sheets": ["Sheet1"]}},
    )
    client = MultiEndpointMcpClient(
        clients={
            "files": files_client,
            "excel": excel_client,
        }
    )

    assert client.list_tools() == ["excel.open_file", "fs_list_entries"]
    assert client.describe_tools() == [
        {"name": "excel.open_file", "description": "", "server": "excel"},
        {"name": "fs_list_entries", "description": "", "server": "files"},
    ]
    assert client.call_tool("excel.open_file", {"file": "sales.xlsx"}) == {"sheets": ["Sheet1"]}
    assert excel_client.calls == [("excel.open_file", {"file": "sales.xlsx"})]
    assert files_client.calls == []


def test_multi_endpoint_client_rejects_duplicate_tool_names() -> None:
    client = MultiEndpointMcpClient(
        clients={
            "files-a": StubMcpClient(["fs_read_text"], {"fs_read_text": {"content": "a"}}),
            "files-b": StubMcpClient(["fs_read_text"], {"fs_read_text": {"content": "b"}}),
        }
    )

    with pytest.raises(ValueError, match="Duplicate MCP tool registrations detected"):
        client.list_tools()
