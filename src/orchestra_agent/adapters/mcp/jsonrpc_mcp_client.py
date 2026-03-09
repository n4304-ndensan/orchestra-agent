from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx

from orchestra_agent.ports.mcp_client import IMcpClient


class JsonRpcMcpClient(IMcpClient):
    def __init__(self, endpoint: str, timeout_seconds: float = 30.0) -> None:
        self._endpoint = endpoint
        self._client = httpx.Client(timeout=timeout_seconds)

    def list_tools(self) -> list[str]:
        return [tool["name"] for tool in self.describe_tools()]

    def describe_tools(self) -> list[dict[str, str]]:
        result = self._request("tools/list", {})
        tools = result.get("tools", [])
        described_tools: list[dict[str, str]] = []
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name")
                if not isinstance(name, str):
                    continue
                description = tool.get("description")
                described_tools.append(
                    {
                        "name": name,
                        "description": description if isinstance(description, str) else "",
                    }
                )
            elif isinstance(tool, str):
                described_tools.append({"name": tool, "description": ""})
        return described_tools

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
        response = self._client.post(self._endpoint, json=payload)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"MCP error for {method}: {body['error']}")
        result = body.get("result", {})
        if isinstance(result, dict):
            return result
        return {"value": result}

