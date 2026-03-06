import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestra_agent.adapters.db import (
    InMemoryAuditLogger,
    InMemoryStepPlanRepository,
    InMemoryWorkflowRepository,
    PostgresAgentStateStore,
)
from orchestra_agent.adapters.planner import LlmPlanner
from orchestra_agent.adapters.policy import DefaultPolicyEngine
from orchestra_agent.adapters.snapshot import FilesystemSnapshotManager
from orchestra_agent.api import ApprovalAPI, RunAPI, WorkflowAPI
from orchestra_agent.application.use_cases import (
    ApproveStepPlanUseCase,
    CompileStepPlanUseCase,
    CreateWorkflowUseCase,
    ExecutePlanUseCase,
)
from orchestra_agent.executor import FailureHandler, PlanExecutor


class FakeExcelMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._last_total = 0

    def list_tools(self) -> list[str]:
        return []

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_ref, dict(input)))

        if tool_ref == "excel.open_file":
            return {"opened": input["file"]}
        if tool_ref == "excel.read_sheet":
            return {"rows": [{"C": 10}, {"C": 20}, {"C": 30}]}
        if tool_ref == "excel.calculate_sum":
            self._last_total = 60
            return {"total": self._last_total}
        if tool_ref == "excel.create_sheet":
            return {"created": input["sheet"]}
        if tool_ref == "excel.write_cells":
            cells = input.get("cells", {})
            if cells.get("B2") != self._last_total:
                raise RuntimeError("write_cells expected resolved total in B2")
            return {"written_cells": len(cells)}
        if tool_ref == "excel.save_file":
            output = Path(input["output"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("summary", encoding="utf-8")
            return {"output": str(output)}
        raise KeyError(f"Unsupported fake tool '{tool_ref}'")


def test_excel_workflow_end_to_end_with_approval_resume() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source_file = base / "sales.xlsx"
        output_file = base / "summary.xlsx"
        source_file.write_text("dummy", encoding="utf-8")

        workflow_repo = InMemoryWorkflowRepository()
        step_plan_repo = InMemoryStepPlanRepository()
        state_store = PostgresAgentStateStore()
        audit_logger = InMemoryAuditLogger()
        planner = LlmPlanner()
        policy_engine = DefaultPolicyEngine()
        snapshot_manager = FilesystemSnapshotManager(base / "snapshots", workspace_root=base)
        mcp_client = FakeExcelMcpClient()

        compile_uc = CompileStepPlanUseCase(planner, policy_engine, step_plan_repo, audit_logger)
        create_workflow_uc = CreateWorkflowUseCase(workflow_repo, audit_logger)
        approve_uc = ApproveStepPlanUseCase(step_plan_repo, audit_logger)
        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=planner,
            policy_engine=policy_engine,
            step_plan_repository=step_plan_repo,
            audit_logger=audit_logger,
            workflow_repository=workflow_repo,
        )
        executor = PlanExecutor(
            mcp_client=mcp_client,
            state_store=state_store,
            snapshot_manager=snapshot_manager,
            audit_logger=audit_logger,
            failure_handler=failure_handler,
        )
        execute_uc = ExecutePlanUseCase(executor, state_store, audit_logger)

        workflow_api = WorkflowAPI(create_workflow_uc, compile_uc, workflow_repo)
        approval_api = ApprovalAPI(approve_uc, step_plan_repo)
        run_api = RunAPI(execute_uc, workflow_repo, step_plan_repo, state_store)

        created = workflow_api.create_workflow(
            name="Excel summary",
            objective=(
                f"Open Excel file {source_file.name}, calculate totals for column C, "
                f"create summary sheet, and export as {output_file.name}"
            ),
        )
        plan_response = workflow_api.generate_step_plan(created["workflow_id"])
        latest_plan = step_plan_repo.get(plan_response["step_plan_id"])
        assert latest_plan is not None
        for step in latest_plan.steps:
            if "file" in step.resolved_input:
                step.resolved_input["file"] = str(source_file)
            if "output" in step.resolved_input:
                step.resolved_input["output"] = str(output_file)
        step_plan_repo.save(latest_plan)

        approval = approval_api.approve_step_plan(step_plan_id=plan_response["step_plan_id"])
        assert approval["approval_status"] == "APPROVED"

        paused = run_api.start_run(
            workflow_id=created["workflow_id"],
            step_plan_id=plan_response["step_plan_id"],
            run_id="run-1",
            approved=False,
        )
        assert paused["approval_status"] == "PENDING"
        assert paused["current_step_id"] == "save_file"

        resumed = run_api.resume_run("run-1", approved=True)
        assert resumed["current_step_id"] is None
        assert resumed["execution_history"][-1]["step_id"] == "save_file"
        assert resumed["execution_history"][-1]["status"] == "SUCCESS"
        assert output_file.is_file()
    finally:
        shutil.rmtree(base, ignore_errors=True)
