from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx

from orchestra_agent.ports.mcp_client import IMcpClient
from orchestra_agent.shared.mcp_tool_catalog import normalize_mcp_tool_catalog


class JsonRpcMcpClient(IMcpClient):
    def __init__(self, endpoint: str, timeout_seconds: float = 30.0) -> None:
        self._endpoint = endpoint
        self._client = httpx.Client(timeout=timeout_seconds)
        self._tool_catalog_cache: list[dict[str, Any]] | None = None

    def list_tools(self) -> list[str]:
        return [tool["name"] for tool in self.describe_tools()]

    def describe_tools(self) -> list[dict[str, Any]]:
        if self._tool_catalog_cache is not None:
            return [dict(tool) for tool in self._tool_catalog_cache]
        result = self._request("tools/list", {})
        tools = result.get("tools", [])
        self._tool_catalog_cache = normalize_mcp_tool_catalog(tools)
        return [dict(tool) for tool in self._tool_catalog_cache]

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "tools/call",
            {
                "name": tool_ref,
                "arguments": input,
            },
        )
        if not isinstance(result, dict):
            return {"value": result}
        return result

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid4().hex
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            response = self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"MCP endpoint request timed out for {method}: {self._endpoint}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                "MCP endpoint returned "
                f"HTTP {exc.response.status_code} for {method}: {self._endpoint}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"MCP endpoint request failed for {method}: {self._endpoint}"
            ) from exc
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"MCP endpoint returned invalid JSON for {method}: {self._endpoint}"
            ) from exc
        if "error" in body:
            raise RuntimeError(f"MCP error for {method}: {body['error']}")
        result = body.get("result", {})
        if isinstance(result, dict):
            return result
        return {"value": result}

