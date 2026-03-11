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
        self._described_tools_cache: list[dict[str, Any]] | None = None
        self._last_tool_discovery_errors: dict[str, str] = {}

    def list_tools(self) -> list[str]:
        if self._described_tools_cache is not None:
            self._tool_owners = {
                tool["name"]: str(tool["server"])
                for tool in self._described_tools_cache
                if isinstance(tool.get("name"), str) and isinstance(tool.get("server"), str)
            }
            return sorted(self._tool_owners)
        tool_catalog: dict[str, dict[str, Any]] = {}
        self._tool_owners = self._discover_tool_owners(tool_catalog=tool_catalog)
        if not self._last_tool_discovery_errors:
            self._described_tools_cache = [tool_catalog[name] for name in sorted(tool_catalog)]
        return sorted(self._tool_owners)

    def describe_tools(self) -> list[dict[str, Any]]:
        if self._described_tools_cache is not None:
            return [dict(tool) for tool in self._described_tools_cache]
        tool_catalog: dict[str, dict[str, Any]] = {}
        self._tool_owners = self._discover_tool_owners(tool_catalog=tool_catalog)
        described_tools = [tool_catalog[name] for name in sorted(tool_catalog)]
        if not self._last_tool_discovery_errors:
            self._described_tools_cache = described_tools
        return [dict(tool) for tool in described_tools]

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

    @property
    def last_tool_discovery_errors(self) -> dict[str, str]:
        return dict(self._last_tool_discovery_errors)

    def _discover_tool_owners(
        self,
        tool_catalog: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        owners: dict[str, str] = {}
        duplicates: dict[str, list[str]] = {}
        discovery_errors: dict[str, str] = {}

        for label, client in self._clients.items():
            try:
                described_tools = self._describe_client_tools(client)
            except Exception as exc:  # noqa: BLE001
                discovery_errors[label] = str(exc)
                continue

            for tool in described_tools:
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
        self._last_tool_discovery_errors = discovery_errors
        if not owners and discovery_errors:
            details = "; ".join(
                f"{label}: {message}" for label, message in sorted(discovery_errors.items())
            )
            raise RuntimeError(
                "MCP tool discovery failed for every configured server. "
                f"{details}"
            )
        return owners

    @staticmethod
    def _describe_client_tools(client: IMcpClient) -> list[dict[str, Any]]:
        describe_tools = getattr(client, "describe_tools", None)
        if callable(describe_tools):
            described_tools = normalize_mcp_tool_catalog(describe_tools())
            if described_tools:
                return described_tools

        return normalize_mcp_tool_catalog(client.list_tools())
