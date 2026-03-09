from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from orchestra_agent.cli import main as run_cli
from orchestra_agent.control_plane import ControlPlaneRequestHandler, ControlPlaneServer
from orchestra_agent.mcp_server.jsonrpc_server import JsonRpcMcpHttpServer, JsonRpcMcpRequestHandler
from orchestra_agent.runtime import RuntimeConfig, build_runtime

openpyxl = pytest.importorskip("openpyxl")


@pytest.fixture()
def workspace_dir() -> Iterator[Path]:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def mcp_endpoint(workspace_dir: Path) -> Iterator[str]:
    server = JsonRpcMcpHttpServer(
        ("127.0.0.1", 0),
        JsonRpcMcpRequestHandler,
        workspace_root=workspace_dir,
        rpc_path="/mcp",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture()
def control_plane_base_url(workspace_dir: Path, mcp_endpoint: str) -> Iterator[str]:
    runtime = build_runtime(
        RuntimeConfig(
            workspace=workspace_dir,
            workflow_root=workspace_dir / "workflow",
            plan_root=workspace_dir / "plan",
            snapshots_dir=workspace_dir / ".orchestra_snapshots",
            state_root=workspace_dir / ".orchestra_state" / "runs",
            audit_root=workspace_dir / ".orchestra_state" / "audit",
            mcp_endpoint=mcp_endpoint,
        )
    )
    server = ControlPlaneServer(
        ("127.0.0.1", 0),
        ControlPlaneRequestHandler,
        runtime=runtime,
        workspace=workspace_dir,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        runtime.close()


def test_cli_runs_against_real_http_excel_mcp(workspace_dir: Path, mcp_endpoint: str) -> None:
    _create_sales_workbook(workspace_dir / "sales.xlsx")

    exit_code = run_cli(
        [
            "sales.xlsxのC列を集計してsummary.xlsxへ",
            "--workspace",
            str(workspace_dir),
            "--mcp-endpoint",
            mcp_endpoint,
            "--run-id",
            "run-real-http",
            "--no-print-plan",
        ]
    )

    assert exit_code == 0

    summary_book = openpyxl.load_workbook(workspace_dir / "summary.xlsx")
    try:
        assert summary_book["Summary"]["B2"].value == 60
    finally:
        summary_book.close()

    run_state = json.loads(
        (workspace_dir / ".orchestra_state" / "runs" / "run-real-http.json").read_text(
            encoding="utf-8"
        )
    )
    assert run_state["approval_status"] == "APPROVED"

    audit_lines = (
        workspace_dir / ".orchestra_state" / "audit" / "events.ndjson"
    ).read_text(encoding="utf-8")
    assert "plan_complete" in audit_lines


def test_control_plane_api_executes_real_http_excel_flow(
    workspace_dir: Path,
    control_plane_base_url: str,
) -> None:
    _create_sales_workbook(workspace_dir / "sales.xlsx")

    with httpx.Client(base_url=control_plane_base_url, timeout=30.0) as client:
        created = client.post(
            "/workflows",
            json={
                "name": "Excel summary",
                "objective": "sales.xlsxのC列を集計してsummary.xlsxへ",
            },
        )
        assert created.status_code == 201
        workflow_id = created.json()["workflow_id"]

        workflow_details = client.get(f"/workflows/{workflow_id}")
        assert workflow_details.status_code == 200
        assert workflow_details.json()["objective"] == "sales.xlsxのC列を集計してsummary.xlsxへ"

        plan_response = client.post(f"/workflows/{workflow_id}/plans", json={})
        assert plan_response.status_code == 201
        step_plan_id = plan_response.json()["step_plan_id"]

        plan_details = client.get(f"/plans/{step_plan_id}")
        assert plan_details.status_code == 200
        assert plan_details.json()["steps"][-1]["tool_ref"] == "excel.save_file"

        approval = client.post(f"/plans/{step_plan_id}/approve", json={})
        assert approval.status_code == 200
        assert approval.json()["approval_status"] == "APPROVED"

        started = client.post(
            "/runs",
            json={
                "workflow_id": workflow_id,
                "step_plan_id": step_plan_id,
                "run_id": "run-control-plane",
                "approved": False,
            },
        )
        assert started.status_code == 201
        run = started.json()
        for _ in range(30):
            if run["approval_status"] == "APPROVED" and run["current_step_id"] is None:
                break
            response = client.post(f"/runs/{run['run_id']}/approval", json={"approve": True})
            assert response.status_code == 200
            run = response.json()

        assert run["approval_status"] == "APPROVED"
        assert run["current_step_id"] is None

        audit_response = client.get("/runs/run-control-plane/audit?limit=20")
        assert audit_response.status_code == 200
        assert audit_response.json()["events"]

    summary_book = openpyxl.load_workbook(workspace_dir / "summary.xlsx")
    try:
        assert summary_book["Summary"]["B2"].value == 60
    finally:
        summary_book.close()


def _create_sales_workbook(path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = "Region"
    sheet["C1"] = "Amount"
    sheet["A2"] = "APAC"
    sheet["C2"] = 10
    sheet["A3"] = "EMEA"
    sheet["C3"] = 20
    sheet["A4"] = "AMER"
    sheet["C4"] = 30
    workbook.save(path)
    workbook.close()
