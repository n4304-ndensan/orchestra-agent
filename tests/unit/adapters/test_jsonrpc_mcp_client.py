from __future__ import annotations

import httpx
import pytest

from orchestra_agent.adapters.mcp import JsonRpcMcpClient


def test_jsonrpc_mcp_client_wraps_http_status_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, json={"error": "down"})

    client = JsonRpcMcpClient("https://example.com/mcp")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)  # noqa: SLF001
    try:
        with pytest.raises(RuntimeError, match="HTTP 503"):
            client.list_tools()
    finally:
        client.close()


def test_jsonrpc_mcp_client_wraps_invalid_json_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"not-json")

    client = JsonRpcMcpClient("https://example.com/mcp")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)  # noqa: SLF001
    try:
        with pytest.raises(RuntimeError, match="invalid JSON"):
            client.list_tools()
    finally:
        client.close()


def test_jsonrpc_mcp_client_caches_describe_tools_response() -> None:
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        seen_methods.append(payload)
        return httpx.Response(
            200,
            request=request,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {
                    "tools": [
                        {"name": "excel.open_file", "description": "Open workbook"},
                    ]
                },
            },
        )

    client = JsonRpcMcpClient("https://example.com/mcp")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)  # noqa: SLF001
    try:
        assert client.describe_tools() == [
            {"name": "excel.open_file", "description": "Open workbook"}
        ]
        assert client.list_tools() == ["excel.open_file"]
        assert len(seen_methods) == 1
    finally:
        client.close()
