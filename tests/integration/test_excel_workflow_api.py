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


class MutatingExcelMcpClient(FakeExcelMcpClient):
    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        result = super().call_tool(tool_ref, input)
        if tool_ref == "excel.create_sheet":
            Path(input["file"]).write_text("mutated-by-create-sheet", encoding="utf-8")
        return result


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
        assert paused["current_step_id"] == "open_file"
        assert paused["pending_approval"]["stage"] == "PRE_STEP"

        resumed = paused
        for _ in range(30):
            if resumed["current_step_id"] is None and resumed["approval_status"] == "APPROVED":
                break
            resumed = run_api.resume_run("run-1", approved=True)

        assert resumed["current_step_id"] is None
        assert resumed["execution_history"][-1]["step_id"] == "save_file"
        assert resumed["execution_history"][-1]["status"] == "SUCCESS"
        assert resumed["metadata"]["artifacts_locked"] is True
        assert output_file.is_file()

        locked_workflow = workflow_repo.get(created["workflow_id"])
        assert locked_workflow is not None
        with_raised = False
        try:
            workflow_repo.save(locked_workflow.with_feedback("should fail"))
        except PermissionError:
            with_raised = True
        assert with_raised is True
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_excel_workflow_feedback_restores_backup_and_replans() -> None:
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
        mcp_client = MutatingExcelMcpClient()

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
        _ = approval_api.approve_step_plan(step_plan_id=plan_response["step_plan_id"])

        run = run_api.start_run(
            workflow_id=created["workflow_id"],
            step_plan_id=plan_response["step_plan_id"],
            run_id="run-feedback",
            approved=False,
        )
        for _ in range(30):
            pending = run.get("pending_approval")
            if (
                isinstance(pending, dict)
                and pending.get("stage") == "POST_STEP"
                and pending.get("step_id") == "create_summary_sheet"
            ):
                break
            run = run_api.resume_run("run-feedback", approved=True)

        assert source_file.read_text(encoding="utf-8") == "mutated-by-create-sheet"
        updated = run_api.submit_feedback(
            run_id="run-feedback",
            feedback="create_summary_sheetの結果が想定と違うので手順を修正してください",
        )

        assert source_file.read_text(encoding="utf-8") == "dummy"
        assert updated["workflow_version"] == 2
        assert updated["step_plan_version"] == 2
        assert updated["approval_status"] == "PENDING"

        wf_v2 = workflow_repo.get(created["workflow_id"], version=2)
        assert wf_v2 is not None
        assert len(wf_v2.feedback_history) >= 1
        assert "create_summary_sheet" in wf_v2.feedback_history[-1]
    finally:
        shutil.rmtree(base, ignore_errors=True)
