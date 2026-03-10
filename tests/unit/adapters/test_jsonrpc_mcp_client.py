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
