from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from orchestra_agent.adapters.mcp.mock_excel_mcp_client import MockExcelMcpClient
from orchestra_agent.control_plane_inspector import ControlPlaneInspector
from orchestra_agent.runtime import RuntimeArtifacts, RuntimeMetadata


class BrokenMcpClient:
    def describe_tools(self) -> list[dict[str, str]]:
        raise RuntimeError("tool discovery failed")


def test_readiness_payload_reports_mock_runtime_details() -> None:
    inspector = ControlPlaneInspector(
        runtime=_runtime_stub(mcp_client=MockExcelMcpClient(), using_mock=True),
        workspace=Path("workspace").resolve(),
        started_at=datetime(2026, 3, 11, 10, 0, tzinfo=UTC),
    )

    status, payload = inspector.readiness_payload()

    assert status == HTTPStatus.OK
    assert payload["status"] == "ready"
    assert payload["runtime"]["mcp_mode"] == "mock"
    assert payload["checks"][0]["tool_count"] >= 1
    assert payload["checks"][0]["detail"] == "mock mcp client is active"


def test_system_payload_and_readiness_report_tool_discovery_failure() -> None:
    inspector = ControlPlaneInspector(
        runtime=_runtime_stub(mcp_client=BrokenMcpClient(), using_mock=False),
        workspace=Path("workspace").resolve(),
        started_at=datetime(2026, 3, 11, 10, 0, tzinfo=UTC),
    )

    status, readiness_payload = inspector.readiness_payload()
    system_payload = inspector.system_payload()

    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert readiness_payload["checks"][0]["status"] == "error"
    assert readiness_payload["checks"][0]["error"] == "tool discovery failed"
    assert system_payload["tools"] == []
    assert system_payload["tool_catalog_error"] == "tool discovery failed"


def _runtime_stub(*, mcp_client: object, using_mock: bool) -> SimpleNamespace:
    return SimpleNamespace(
        mcp_client=mcp_client,
        using_mock=using_mock,
        metadata=RuntimeMetadata(
            app_version="0.1.0",
            llm_provider="none",
            planner_mode="deterministic",
            mcp_endpoints=("http://127.0.0.1:8010/mcp",),
        ),
        artifacts=RuntimeArtifacts(
            workspace_root=Path("workspace").resolve(),
            workflow_root=Path("workspace/workflow").resolve(),
            plan_root=Path("workspace/plan").resolve(),
            snapshots_dir=Path("workspace/.orchestra_snapshots").resolve(),
            state_root=Path("workspace/.orchestra_state/runs").resolve(),
            audit_root=Path("workspace/.orchestra_state/audit").resolve(),
        ),
    )
