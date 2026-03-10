import shutil
from dataclasses import replace
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
from orchestra_agent.domain import AgentState, ApprovalStatus, Step, StepPlan, Workflow
from orchestra_agent.domain.enums import RiskLevel
from orchestra_agent.executor import FailureHandler, PlanExecutor
from orchestra_agent.ports import IMcpClient, IStepExecutor


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


class AliasFriendlyExcelMcpClient(FakeExcelMcpClient):
    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_ref, dict(input)))
        if tool_ref == "excel.create_file":
            file_path = Path(input["file"])
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("workbook", encoding="utf-8")
            return {"file": str(file_path)}
        if tool_ref == "excel.write_cells":
            cells = input.get("cells", {})
            return {"written_cells": len(cells) if isinstance(cells, dict) else 0}
        if tool_ref == "excel.save_file":
            output = Path(input["output"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("summary", encoding="utf-8")
            return {"output": str(output)}
        raise KeyError(f"Unsupported fake tool '{tool_ref}'")


class RecordingAgenticExecutor(IStepExecutor):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        mcp_client: IMcpClient,
    ) -> dict[str, Any]:
        self.calls.append((step.step_id, dict(resolved_input)))
        if step.step_id == "calculate_totals":
            return {"step_id": step.step_id, "status": "agentic", "total": 60}
        return {"step_id": step.step_id, "status": "agentic"}


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
    *,
    max_replans: int = 1,
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
        max_replans=max_replans,
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
        assert len(state.snapshot_refs) == 6
        assert all(record.snapshot_ref is not None for record in state.execution_history)
        assert (base / "summary.xlsx").is_file()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_only_pauses_on_steps_that_require_runtime_approval() -> None:
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
        assert paused_pre.current_step_id is None
        assert len(paused_pre.execution_history) == 0
        plan_context = paused_pre.metadata.get("approval_context")
        assert isinstance(plan_context, dict)
        assert plan_context.get("stage") == "PLAN"
        assert plan_context.get("step_id") == "__plan__"

        paused_pre = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert paused_pre.approval_status == ApprovalStatus.PENDING
        assert paused_pre.current_step_id == "save_file"
        assert len(paused_pre.execution_history) == 5
        pre_context = paused_pre.metadata.get("approval_context")
        assert isinstance(pre_context, dict)
        assert pre_context.get("stage") == "PRE_STEP"
        assert pre_context.get("step_id") == "save_file"

        paused_post = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert paused_post.approval_status == ApprovalStatus.PENDING
        assert paused_post.current_step_id == "save_file"
        assert len(paused_post.execution_history) == 6
        post_context = paused_post.metadata.get("approval_context")
        assert isinstance(post_context, dict)
        assert post_context.get("stage") == "POST_STEP"
        assert post_context.get("step_id") == "save_file"

        next_pre = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-approval",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert next_pre.approval_status == ApprovalStatus.APPROVED
        assert next_pre.current_step_id is None
        assert len(next_pre.execution_history) == 6
        assert next_pre.metadata.get("approval_context") is None
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


def test_plan_executor_routes_standard_steps_through_agentic_executor() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(base, FakeExcelMcpClient())
        agentic_executor = RecordingAgenticExecutor()
        state_store = PostgresAgentStateStore()
        step_plan_repo = InMemoryStepPlanRepository()
        workflow_repo = InMemoryWorkflowRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(
            base / "snapshots-agentic",
            workspace_root=base,
        )
        workflow_repo.save(workflow)
        step_plan_repo.save(plan)
        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=LlmPlanner(),
            policy_engine=DefaultPolicyEngine(),
            step_plan_repository=step_plan_repo,
            audit_logger=audit_logger,
            workflow_repository=workflow_repo,
            max_replans=1,
        )
        executor = PlanExecutor(
            mcp_client=FakeExcelMcpClient(),
            state_store=state_store,
            snapshot_manager=snapshot_manager,
            audit_logger=audit_logger,
            failure_handler=failure_handler,
            step_executor=agentic_executor,
        )
        use_case = ExecutePlanUseCase(executor, state_store, audit_logger)

        state = _auto_approve_until_done(
            execute_use_case=use_case,
            workflow=workflow,
            step_plan=plan,
            run_id="run-agentic",
        )

        assert state.approval_status == ApprovalStatus.APPROVED
        assert len(agentic_executor.calls) == 6
        assert agentic_executor.calls[0][0] == "open_file"
        assert state.execution_history[0].result == {"step_id": "open_file", "status": "agentic"}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_respects_explicit_requires_approval_on_low_risk_step() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(base, FakeExcelMcpClient())
        gated_steps = [
            replace(
                step,
                requires_approval=(step.step_id == "open_file"),
                risk_level=step.risk_level if step.step_id == "save_file" else RiskLevel.LOW,
            )
            if step.step_id != "save_file"
            else replace(step, requires_approval=False)
            for step in plan.steps
        ]
        gated_plan = StepPlan(
            step_plan_id=plan.step_plan_id,
            workflow_id=plan.workflow_id,
            version=plan.version,
            steps=gated_steps,
        )

        state_store = PostgresAgentStateStore()
        step_plan_repo = InMemoryStepPlanRepository()
        workflow_repo = InMemoryWorkflowRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(
            base / "snapshots-explicit",
            workspace_root=base,
        )
        workflow_repo.save(workflow)
        step_plan_repo.save(gated_plan)
        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=LlmPlanner(),
            policy_engine=DefaultPolicyEngine(),
            step_plan_repository=step_plan_repo,
            audit_logger=audit_logger,
            workflow_repository=workflow_repo,
            max_replans=1,
        )
        executor = PlanExecutor(
            mcp_client=FakeExcelMcpClient(),
            state_store=state_store,
            snapshot_manager=snapshot_manager,
            audit_logger=audit_logger,
            failure_handler=failure_handler,
        )
        use_case = ExecutePlanUseCase(executor, state_store, audit_logger)

        state = use_case.execute(
            workflow=workflow,
            step_plan=gated_plan,
            run_id="run-explicit-approval",
            approval_status=ApprovalStatus.PENDING,
        )
        assert state.metadata["approval_context"]["stage"] == "PLAN"
        assert "details" in state.metadata["approval_context"]
        assert any(
            isinstance(line, str) and "open_file" in line
            for line in state.metadata["approval_context"]["details"]
        )

        state = use_case.execute(
            workflow=workflow,
            step_plan=gated_plan,
            run_id="run-explicit-approval",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert state.metadata["approval_context"]["stage"] == "PRE_STEP"
        assert state.metadata["approval_context"]["step_id"] == "open_file"
        assert any(
            isinstance(line, str) and "tool" in line
            for line in state.metadata["approval_context"]["details"]
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_normalizes_excel_alias_inputs_before_mcp_call() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source_file = base / "hello.xlsx"
        workflow = Workflow(
            workflow_id="wf-alias",
            name="Alias workflow",
            version=1,
            objective="Create HelloWorld workbook",
        )
        plan = StepPlan(
            step_plan_id="sp-alias",
            workflow_id=workflow.workflow_id,
            version=workflow.version,
            steps=[
                Step(
                    step_id="create_excel_file",
                    name="Create HelloWorld workbook",
                    description="Create a workbook using alias inputs.",
                    tool_ref="excel.create_file",
                    resolved_input={"path": str(source_file)},
                ),
                Step(
                    step_id="write_hello",
                    name="Write A1",
                    description="Write HelloWorld to A1 using alias sheet input.",
                    tool_ref="excel.write_cells",
                    resolved_input={
                        "path": str(source_file),
                        "sheet_name": "Sheet1",
                        "cells": {"A1": "HelloWorld"},
                    },
                    depends_on=["create_excel_file"],
                ),
                Step(
                    step_id="save_excel_file",
                    name="Save workbook",
                    description="Save in place when output is omitted.",
                    tool_ref="excel.save_file",
                    resolved_input={"path": str(source_file)},
                    depends_on=["write_hello"],
                ),
            ],
        )

        state_store = PostgresAgentStateStore()
        step_plan_repo = InMemoryStepPlanRepository()
        workflow_repo = InMemoryWorkflowRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(base / "snapshots", workspace_root=base)
        workflow_repo.save(workflow)
        step_plan_repo.save(plan)

        mcp_client = AliasFriendlyExcelMcpClient()
        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=LlmPlanner(),
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
        use_case = ExecutePlanUseCase(executor, state_store, audit_logger)

        state = _auto_approve_until_done(
            execute_use_case=use_case,
            workflow=workflow,
            step_plan=plan,
            run_id="run-alias",
        )

        assert state.approval_status == ApprovalStatus.APPROVED
        assert mcp_client.calls == [
            ("excel.create_file", {"file": str(source_file)}),
            (
                "excel.write_cells",
                {
                    "file": str(source_file),
                    "sheet": "Sheet1",
                    "cells": {"A1": "HelloWorld"},
                },
            ),
            (
                "excel.save_file",
                {
                    "file": str(source_file),
                    "output": str(source_file),
                },
            ),
        ]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_skips_plan_review_when_plan_has_no_runtime_approval() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(base, FakeExcelMcpClient())
        low_risk_steps = [
            replace(step, risk_level=RiskLevel.LOW, requires_approval=False)
            for step in plan.steps
        ]
        low_risk_plan = StepPlan(
            step_plan_id=plan.step_plan_id,
            workflow_id=plan.workflow_id,
            version=plan.version,
            steps=low_risk_steps,
        )

        state_store = PostgresAgentStateStore()
        step_plan_repo = InMemoryStepPlanRepository()
        workflow_repo = InMemoryWorkflowRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(
            base / "snapshots-no-plan-approval",
            workspace_root=base,
        )
        workflow_repo.save(workflow)
        step_plan_repo.save(low_risk_plan)
        failure_handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=LlmPlanner(),
            policy_engine=DefaultPolicyEngine(),
            step_plan_repository=step_plan_repo,
            audit_logger=audit_logger,
            workflow_repository=workflow_repo,
            max_replans=1,
        )
        executor = PlanExecutor(
            mcp_client=FakeExcelMcpClient(),
            state_store=state_store,
            snapshot_manager=snapshot_manager,
            audit_logger=audit_logger,
            failure_handler=failure_handler,
        )
        use_case = ExecutePlanUseCase(executor, state_store, audit_logger)

        state = use_case.execute(
            workflow=workflow,
            step_plan=low_risk_plan,
            run_id="run-no-plan-approval",
            approval_status=ApprovalStatus.PENDING,
        )

        assert state.approval_status == ApprovalStatus.APPROVED
        assert state.current_step_id is None
        assert len(state.execution_history) == 6
        assert state.metadata.get("approval_context") is None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_accepts_feedback_during_plan_review() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(base, FakeExcelMcpClient())

        state = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-plan-feedback",
            approval_status=ApprovalStatus.PENDING,
        )
        context = state.metadata.get("approval_context")
        assert isinstance(context, dict)
        assert context.get("stage") == "PLAN"

        updated = execute_use_case.apply_feedback(
            workflow=workflow,
            step_plan=plan,
            run_id="run-plan-feedback",
            feedback="save_file の output 名を output/HelloWorld.xlsx にしてください",
        )

        assert updated.workflow_version == 2
        assert updated.step_plan_version == 2
        assert updated.approval_status == ApprovalStatus.PENDING
        assert updated.last_error is None
        assert updated.metadata.get("feedback_step_id") == "plan"
        updated_context = updated.metadata.get("approval_context")
        assert isinstance(updated_context, dict)
        assert updated_context.get("stage") == "PLAN"
        assert updated_context.get("step_id") == "__plan__"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_accepts_feedback_during_pre_step_review() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(base, FakeExcelMcpClient())

        _ = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-pre-feedback",
            approval_status=ApprovalStatus.PENDING,
        )
        paused_pre = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-pre-feedback",
            approval_status=ApprovalStatus.APPROVED,
        )
        context = paused_pre.metadata.get("approval_context")
        assert isinstance(context, dict)
        assert context.get("stage") == "PRE_STEP"
        assert context.get("step_id") == "save_file"

        updated = execute_use_case.apply_feedback(
            workflow=workflow,
            step_plan=plan,
            run_id="run-pre-feedback",
            feedback="save_file 前に別シートのチェック step を追加して",
        )

        assert updated.workflow_version == 2
        assert updated.step_plan_version == 2
        assert updated.approval_status == ApprovalStatus.PENDING
        assert updated.last_error is None
        assert updated.metadata.get("feedback_step_id") == "save_file"
        updated_context = updated.metadata.get("approval_context")
        assert isinstance(updated_context, dict)
        assert updated_context.get("stage") == "PLAN"
        assert updated_context.get("step_id") == "__plan__"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_executor_accepts_feedback_after_failure_without_approval_context() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        execute_use_case, workflow, plan = _build_use_case(
            base,
            FakeExcelMcpClient(fail_tools={"excel.open_file"}),
            max_replans=0,
        )

        _ = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-failed-feedback",
            approval_status=ApprovalStatus.PENDING,
        )
        failed = execute_use_case.execute(
            workflow=workflow,
            step_plan=plan,
            run_id="run-failed-feedback",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert failed.approval_status == ApprovalStatus.REJECTED
        assert failed.metadata.get("approval_context") is None
        assert failed.last_error is not None

        updated = execute_use_case.apply_feedback(
            workflow=workflow,
            step_plan=plan,
            run_id="run-failed-feedback",
            feedback="open_file の入力パスが誤っているので修正して",
        )

        assert updated.workflow_version == 2
        assert updated.step_plan_version == 2
        assert updated.approval_status == ApprovalStatus.PENDING
        assert updated.last_error is None
        assert updated.metadata.get("feedback_step_id") == "open_file"
        updated_context = updated.metadata.get("approval_context")
        assert isinstance(updated_context, dict)
        assert updated_context.get("stage") == "PLAN"
        assert updated_context.get("step_id") == "__plan__"
    finally:
        shutil.rmtree(base, ignore_errors=True)
