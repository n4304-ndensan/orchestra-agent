from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestra_agent.ports import IMcpClient

if TYPE_CHECKING:
    from orchestra_agent.config import AppConfig


def resolve_path(value: str, workspace: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((workspace / path).resolve())


def resolve_file_arg(value: str, workspace: Path) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        return raw
    if raw.exists():
        return raw.resolve()
    return (workspace / raw).resolve()


def normalize_mcp_endpoints(
    configured_endpoints: Iterable[str],
    legacy_endpoint: str | None = None,
) -> tuple[str, ...]:
    normalized: list[str] = []

    for endpoint in configured_endpoints:
        normalized_endpoint = endpoint.strip()
        if normalized_endpoint and normalized_endpoint not in normalized:
            normalized.append(normalized_endpoint)

    if legacy_endpoint is not None:
        normalized_legacy = legacy_endpoint.strip()
        if normalized_legacy and normalized_legacy not in normalized:
            normalized.append(normalized_legacy)

    return tuple(normalized)


def resolve_mcp_endpoints(
    raw_endpoints: Sequence[str] | None,
    config: AppConfig,
) -> tuple[str, ...]:
    if raw_endpoints is not None:
        return normalize_mcp_endpoints(raw_endpoints)
    return normalize_mcp_endpoints(config.mcp.runtime_endpoints())


def describe_mcp_tools(mcp_client: IMcpClient | Any) -> list[dict[str, str]]:
    describe_tools = getattr(mcp_client, "describe_tools", None)
    if callable(describe_tools):
        raw_tools = describe_tools()
        described_tools: list[dict[str, str]] = []
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, dict):
                continue
            name = raw_tool.get("name")
            if not isinstance(name, str):
                continue
            description = raw_tool.get("description")
            described_tools.append(
                {
                    "name": name,
                    "description": description if isinstance(description, str) else "",
                }
            )
        if described_tools:
            return described_tools

    return [{"name": tool_name, "description": ""} for tool_name in mcp_client.list_tools()]
