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
        self.describe_calls = 0

    def list_tools(self) -> list[str]:
        return list(self._tools)

    def describe_tools(self) -> list[dict[str, str]]:
        self.describe_calls += 1
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


class FailingDescribeMcpClient:
    def __init__(self, message: str) -> None:
        self._message = message

    def list_tools(self) -> list[str]:
        raise RuntimeError(self._message)

    def describe_tools(self) -> list[dict[str, str]]:
        raise RuntimeError(self._message)

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(self._message)


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


def test_multi_endpoint_client_caches_describe_tools_results() -> None:
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

    assert client.describe_tools() == [
        {"name": "excel.open_file", "description": "", "server": "excel"},
        {"name": "fs_list_entries", "description": "", "server": "files"},
    ]
    assert client.describe_tools() == [
        {"name": "excel.open_file", "description": "", "server": "excel"},
        {"name": "fs_list_entries", "description": "", "server": "files"},
    ]
    assert client.list_tools() == ["excel.open_file", "fs_list_entries"]
    assert files_client.describe_calls == 1
    assert excel_client.describe_calls == 1


def test_multi_endpoint_client_returns_partial_tool_catalog_when_one_server_fails() -> None:
    client = MultiEndpointMcpClient(
        clients={
            "files": FailingDescribeMcpClient("MCP endpoint request failed for tools/list: files"),
            "excel": StubMcpClient(
                tools=["excel.open_file"],
                result_by_tool={"excel.open_file": {"sheets": ["Sheet1"]}},
                descriptions={"excel.open_file": "Open workbook"},
            ),
        }
    )

    assert client.describe_tools() == [
        {"name": "excel.open_file", "description": "Open workbook", "server": "excel"},
    ]
    assert client.list_tools() == ["excel.open_file"]
    assert client.last_tool_discovery_errors == {
        "files": "MCP endpoint request failed for tools/list: files"
    }


def test_multi_endpoint_client_raises_when_every_server_fails() -> None:
    client = MultiEndpointMcpClient(
        clients={
            "files": FailingDescribeMcpClient("files down"),
            "excel": FailingDescribeMcpClient("excel down"),
        }
    )

    with pytest.raises(RuntimeError, match="every configured server"):
        client.describe_tools()
