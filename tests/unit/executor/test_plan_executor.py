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
from orchestra_agent.application.use_cases import ExecutePlanUseCase
from orchestra_agent.domain import ApprovalStatus, StepPlan, Workflow
from orchestra_agent.executor import FailureHandler, PlanExecutor


class FakeExcelMcpClient:
    def __init__(self, fail_tools: set[str] | None = None) -> None:
        self.fail_tools = fail_tools or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._last_total = 0

    def list_tools(self) -> list[str]:
        return []

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_ref, dict(input)))
        if tool_ref in self.fail_tools:
            raise RuntimeError(f"forced failure: {tool_ref}")

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


def _rewrite_plan_file_paths(plan_file: Path, output_file: Path, plan: StepPlan) -> None:
    for step in plan.steps:
        file_input = step.resolved_input.get("file")
        if isinstance(file_input, str):
            step.resolved_input["file"] = str(plan_file)
        output_input = step.resolved_input.get("output")
        if isinstance(output_input, str):
            step.resolved_input["output"] = str(output_file)


def test_plan_executor_runs_full_excel_flow() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source_file = base / "sales.xlsx"
        output_file = base / "summary.xlsx"
        source_file.write_text("dummy", encoding="utf-8")

        workflow = Workflow(
            workflow_id="wf-1",
            name="Excel summary",
            version=1,
            objective="Summarize sales.xlsx column C and export as summary.xlsx",
        )
        planner = LlmPlanner()
        plan = planner.compile_step_plan(workflow)
        _rewrite_plan_file_paths(source_file, output_file, plan)

        state_store = PostgresAgentStateStore()
        step_plan_repo = InMemoryStepPlanRepository()
        workflow_repo = InMemoryWorkflowRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(base / "snapshots", workspace_root=base)
        mcp_client = FakeExcelMcpClient()

        workflow_repo.save(workflow)
        step_plan_repo.save(plan)

        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=planner,
            policy_engine=DefaultPolicyEngine(),
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
        execute_use_case = ExecutePlanUseCase(executor, state_store, audit_logger)

        state = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            approval_status=ApprovalStatus.APPROVED,
        )

        statuses = [record.status.value for record in state.execution_history]
        assert statuses == ["SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS"]
        assert output_file.is_file()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_pauses_on_pending_approval_then_resumes() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source_file = base / "sales.xlsx"
        output_file = base / "summary.xlsx"
        source_file.write_text("dummy", encoding="utf-8")

        workflow = Workflow(
            workflow_id="wf-1",
            name="Excel summary",
            version=1,
            objective="Summarize sales.xlsx column C and export as summary.xlsx",
        )
        planner = LlmPlanner()
        plan = planner.compile_step_plan(workflow)
        _rewrite_plan_file_paths(source_file, output_file, plan)

        state_store = PostgresAgentStateStore()
        step_plan_repo = InMemoryStepPlanRepository()
        workflow_repo = InMemoryWorkflowRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(base / "snapshots", workspace_root=base)
        mcp_client = FakeExcelMcpClient()

        workflow_repo.save(workflow)
        step_plan_repo.save(plan)

        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=planner,
            policy_engine=DefaultPolicyEngine(),
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
        execute_use_case = ExecutePlanUseCase(executor, state_store, audit_logger)

        paused = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.PENDING,
        )
        assert paused.approval_status == ApprovalStatus.PENDING
        assert paused.current_step_id == "save_file"
        assert len(paused.execution_history) == 5

        resumed = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert resumed.current_step_id is None
        assert resumed.execution_history[-1].step_id == "save_file"
        assert resumed.execution_history[-1].status.value == "SUCCESS"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_replans_after_failure() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source_file = base / "sales.xlsx"
        output_file = base / "summary.xlsx"
        source_file.write_text("dummy", encoding="utf-8")

        workflow = Workflow(
            workflow_id="wf-1",
            name="Excel summary",
            version=1,
            objective="Summarize sales.xlsx column C and export as summary.xlsx",
        )
        planner = LlmPlanner()
        plan = planner.compile_step_plan(workflow)
        _rewrite_plan_file_paths(source_file, output_file, plan)

        state_store = PostgresAgentStateStore()
        step_plan_repo = InMemoryStepPlanRepository()
        workflow_repo = InMemoryWorkflowRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(base / "snapshots", workspace_root=base)
        mcp_client = FakeExcelMcpClient(fail_tools={"excel.write_cells"})

        workflow_repo.save(workflow)
        step_plan_repo.save(plan)

        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=planner,
            policy_engine=DefaultPolicyEngine(),
            step_plan_repository=step_plan_repo,
            audit_logger=audit_logger,
            workflow_repository=workflow_repo,
            max_replans=1,
        )
        executor = PlanExecutor(
            mcp_client=mcp_client,
            state_store=state_store,
            snapshot_manager=snapshot_manager,
            audit_logger=audit_logger,
            failure_handler=failure_handler,
        )
        execute_use_case = ExecutePlanUseCase(executor, state_store, audit_logger)

        state = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-replan",
            approval_status=ApprovalStatus.APPROVED,
        )

        assert state.workflow_version == 2
        assert state.approval_status == ApprovalStatus.PENDING
        assert state.last_error is not None
    finally:
        shutil.rmtree(base, ignore_errors=True)
