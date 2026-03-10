from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class McpToolDescriptor:
    name: str
    description: str = ""
    server: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
        }
        if self.server is not None:
            payload["server"] = self.server
        payload.update(self.metadata)
        return payload


def normalize_mcp_tool_catalog(
    raw_tools: Iterable[Any],
    *,
    default_server: str | None = None,
) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        descriptor = _normalize_single_tool(raw_tool, default_server=default_server)
        if descriptor is None:
            continue
        descriptors.append(descriptor.to_dict())
    return descriptors


def _normalize_single_tool(
    raw_tool: Any,
    *,
    default_server: str | None = None,
) -> McpToolDescriptor | None:
    if isinstance(raw_tool, str):
        normalized_name = raw_tool.strip()
        if not normalized_name:
            return None
        return McpToolDescriptor(name=normalized_name, server=default_server)

    if not isinstance(raw_tool, Mapping):
        return None

    raw_name = raw_tool.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None

    description = raw_tool.get("description")
    raw_server = raw_tool.get("server")
    server = raw_server if isinstance(raw_server, str) and raw_server.strip() else default_server
    metadata = {
        str(key): value
        for key, value in raw_tool.items()
        if key not in {"name", "description", "server"}
    }
    return McpToolDescriptor(
        name=raw_name.strip(),
        description=description if isinstance(description, str) else "",
        server=server,
        metadata=metadata,
    )
