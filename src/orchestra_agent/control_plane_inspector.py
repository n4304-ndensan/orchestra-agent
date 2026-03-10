from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from orchestra_agent import PACKAGE_NAME, __version__
from orchestra_agent.runtime import AppRuntime, describe_mcp_tools


class ControlPlaneInspector:
    def __init__(
        self,
        runtime: AppRuntime,
        workspace: Path,
        started_at: datetime,
    ) -> None:
        self._runtime = runtime
        self._workspace = workspace
        self._started_at = started_at

    def service_index(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": self.service_payload(),
            "endpoints": {
                "health": "/health",
                "ready": "/ready",
                "system": "/system",
                "tools": "/tools",
                "workflows": "/workflows",
                "runs": "/runs",
            },
        }

    def health_payload(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": self.service_payload(),
            "runtime": self.runtime_payload(),
        }

    def readiness_payload(self) -> tuple[HTTPStatus, dict[str, Any]]:
        tools_check = self.tool_catalog_check()
        ready = tools_check["status"] == "ok"
        return (
            HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "status": "ready" if ready else "not_ready",
                "service": self.service_payload(),
                "runtime": self.runtime_payload(),
                "checks": [tools_check],
            },
        )

    def system_payload(self) -> dict[str, Any]:
        tool_catalog, tool_error = self.tool_catalog_snapshot()
        payload: dict[str, Any] = {
            "service": self.service_payload(),
            "runtime": self.runtime_payload(),
            "storage": self.storage_payload(),
            "tools": tool_catalog,
            "docs": {
                "health": "/health",
                "ready": "/ready",
                "tools": "/tools",
            },
        }
        if tool_error is not None:
            payload["tool_catalog_error"] = tool_error
        return payload

    def service_payload(self) -> dict[str, Any]:
        app_version = self._runtime.metadata.app_version or __version__
        return {
            "name": PACKAGE_NAME,
            "version": app_version,
            "started_at": self._started_at.isoformat(),
            "uptime_seconds": uptime_seconds(self._started_at),
        }

    def runtime_payload(self) -> dict[str, Any]:
        metadata = self._runtime.metadata
        return {
            "workspace": str(self._workspace),
            "llm_provider": metadata.llm_provider,
            "planner_mode": metadata.planner_mode,
            "mcp_mode": "mock" if self._runtime.using_mock else "live",
            "mcp_endpoints": list(metadata.mcp_endpoints),
        }

    def storage_payload(self) -> dict[str, str]:
        artifacts = self._runtime.artifacts
        return {
            "workspace_root": str(artifacts.workspace_root),
            "workflow_root": str(artifacts.workflow_root),
            "plan_root": str(artifacts.plan_root),
            "snapshots_dir": str(artifacts.snapshots_dir),
            "state_root": str(artifacts.state_root),
            "audit_root": str(artifacts.audit_root),
        }

    def tool_catalog_check(self) -> dict[str, Any]:
        try:
            tool_catalog = describe_mcp_tools(self._runtime.mcp_client)
        except Exception as exc:  # noqa: BLE001
            return {
                "name": "mcp_tool_catalog",
                "status": "error",
                "error": str(exc),
            }

        payload: dict[str, Any] = {
            "name": "mcp_tool_catalog",
            "status": "ok",
            "tool_count": len(tool_catalog),
        }
        if self._runtime.using_mock:
            payload["detail"] = "mock mcp client is active"
        return payload

    def tool_catalog_snapshot(self) -> tuple[list[dict[str, str]], str | None]:
        try:
            return describe_mcp_tools(self._runtime.mcp_client), None
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)


def uptime_seconds(started_at: datetime) -> int:
    return max(0, int((datetime.now(UTC) - started_at).total_seconds()))
