from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from orchestra_agent.ports import IMcpClient
from orchestra_agent.shared.mcp_tool_catalog import normalize_mcp_tool_catalog

from .jsonrpc_mcp_client import JsonRpcMcpClient


class MultiEndpointMcpClient(IMcpClient):
    _shared_tool_refs = {"server_ping"}

    def __init__(
        self,
        endpoints: Sequence[str] | None = None,
        *,
        clients: Mapping[str, IMcpClient] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if clients is not None:
            self._clients = dict(clients)
        else:
            if endpoints is None or not endpoints:
                raise ValueError("At least one MCP endpoint is required.")
            self._clients = {
                endpoint: JsonRpcMcpClient(endpoint=endpoint, timeout_seconds=timeout_seconds)
                for endpoint in endpoints
            }
        self._tool_owners: dict[str, str] = {}

    def list_tools(self) -> list[str]:
        self._tool_owners = self._discover_tool_owners()
        return sorted(self._tool_owners)

    def describe_tools(self) -> list[dict[str, Any]]:
        tool_catalog: dict[str, dict[str, Any]] = {}
        self._tool_owners = self._discover_tool_owners(tool_catalog=tool_catalog)
        return [tool_catalog[name] for name in sorted(tool_catalog)]

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        owner = self._tool_owners.get(tool_ref)
        if owner is None:
            self._tool_owners = self._discover_tool_owners()
            owner = self._tool_owners.get(tool_ref)
        if owner is None:
            raise RuntimeError(f"Tool '{tool_ref}' is not available from configured MCP servers.")
        return self._clients[owner].call_tool(tool_ref, input)

    def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _discover_tool_owners(
        self,
        tool_catalog: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        owners: dict[str, str] = {}
        duplicates: dict[str, list[str]] = {}

        for label, client in self._clients.items():
            for tool in self._describe_client_tools(client):
                tool_name = tool["name"]
                existing = owners.get(tool_name)
                if existing is None:
                    owners[tool_name] = label
                    if tool_catalog is not None:
                        annotated_tool = dict(tool)
                        annotated_tool["server"] = label
                        tool_catalog[tool_name] = annotated_tool
                    continue
                if tool_name in self._shared_tool_refs:
                    continue
                duplicates.setdefault(tool_name, [existing]).append(label)

        if duplicates:
            details = ", ".join(
                f"{tool_name} -> {', '.join(labels)}"
                for tool_name, labels in sorted(duplicates.items())
            )
            raise ValueError(f"Duplicate MCP tool registrations detected: {details}")
        return owners

    @staticmethod
    def _describe_client_tools(client: IMcpClient) -> list[dict[str, Any]]:
        describe_tools = getattr(client, "describe_tools", None)
        if callable(describe_tools):
            described_tools = normalize_mcp_tool_catalog(describe_tools())
            if described_tools:
                return described_tools

        return normalize_mcp_tool_catalog(client.list_tools())
