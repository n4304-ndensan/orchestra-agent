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
from orchestra_agent.domain import AgentState, ApprovalStatus, StepPlan, Workflow
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


def _build_use_case(
    base: Path,
    mcp_client: FakeExcelMcpClient,
) -> tuple[ExecutePlanUseCase, Workflow, StepPlan]:
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
    return ExecutePlanUseCase(executor, state_store, audit_logger), workflow, plan


def _auto_approve_until_done(
    execute_use_case: ExecutePlanUseCase,
    workflow: Workflow,
    step_plan: StepPlan,
    run_id: str,
    max_rounds: int = 40,
) -> AgentState:
    state = execute_use_case.execute(
        workflow=workflow,
        step_plan=step_plan,
        run_id=run_id,
        approval_status=ApprovalStatus.PENDING,
    )
    for _ in range(max_rounds):
        approval_context = state.metadata.get("approval_context")
        if (
            state.current_step_id is None
            and state.approval_status == ApprovalStatus.APPROVED
            and not isinstance(approval_context, dict)
        ):
            return state
        state = execute_use_case.execute(
            workflow=workflow,
            step_plan=step_plan,
            run_id=run_id,
            approval_status=ApprovalStatus.APPROVED,
        )
    return state


def test_plan_executor_runs_full_excel_flow() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(base, FakeExcelMcpClient())
        state = _auto_approve_until_done(
            execute_use_case=execute_use_case,
            workflow=workflow,
            step_plan=plan,
            run_id="run-full",
        )

        statuses = [record.status.value for record in state.execution_history]
        assert statuses == ["SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS"]
        assert (base / "summary.xlsx").is_file()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_requires_pre_and_post_approval_for_each_step() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(base, FakeExcelMcpClient())

        paused_pre = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.PENDING,
        )
        assert paused_pre.approval_status == ApprovalStatus.PENDING
        assert paused_pre.current_step_id == "open_file"
        assert len(paused_pre.execution_history) == 0
        pre_context = paused_pre.metadata.get("approval_context")
        assert isinstance(pre_context, dict)
        assert pre_context.get("stage") == "PRE_STEP"
        assert pre_context.get("step_id") == "open_file"

        paused_post = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert paused_post.approval_status == ApprovalStatus.PENDING
        assert paused_post.current_step_id == "open_file"
        assert len(paused_post.execution_history) == 1
        post_context = paused_post.metadata.get("approval_context")
        assert isinstance(post_context, dict)
        assert post_context.get("stage") == "POST_STEP"
        assert post_context.get("step_id") == "open_file"

        next_pre = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert next_pre.approval_status == ApprovalStatus.PENDING
        assert next_pre.current_step_id == "read_sheet"
        assert len(next_pre.execution_history) == 1
        next_context = next_pre.metadata.get("approval_context")
        assert isinstance(next_context, dict)
        assert next_context.get("stage") == "PRE_STEP"
        assert next_context.get("step_id") == "read_sheet"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_replans_after_failure() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(
            base, FakeExcelMcpClient(fail_tools={"excel.write_cells"})
        )

        state = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-replan",
            approval_status=ApprovalStatus.PENDING,
        )
        for _ in range(40):
            if state.workflow_version == 2 and state.approval_status == ApprovalStatus.PENDING:
                break
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
