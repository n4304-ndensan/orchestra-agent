from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from orchestra_agent.adapters.db import (
    InMemoryAuditLogger,
    InMemoryStepPlanRepository,
    InMemoryWorkflowRepository,
)
from orchestra_agent.adapters.planner import LlmPlanner
from orchestra_agent.adapters.policy import DefaultPolicyEngine
from orchestra_agent.adapters.snapshot import FilesystemSnapshotManager
from orchestra_agent.domain import AgentState, StepPlan, Workflow
from orchestra_agent.executor import FailureHandler


class RecordingPlanner:
    def __init__(self) -> None:
        self._delegate = LlmPlanner()
        self.workflows: list[Workflow] = []

    def compile_step_plan(self, workflow: Workflow) -> StepPlan:
        self.workflows.append(workflow)
        return self._delegate.compile_step_plan(workflow)


def test_failure_handler_passes_source_documents_and_change_summary_to_replan() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        workflow = Workflow(
            workflow_id="wf-review",
            name="Review workflow",
            version=1,
            objective="Review sales.xlsx and export summary.xlsx",
            feedback_history=["Initial plan was too coarse."],
        )
        planner = RecordingPlanner()
        step_plan = planner.compile_step_plan(workflow)

        workflow_repo = InMemoryWorkflowRepository()
        workflow_repo.save(workflow)
        step_plan_repo = InMemoryStepPlanRepository()
        audit_logger = InMemoryAuditLogger()
        snapshot_manager = FilesystemSnapshotManager(base / "snapshots", workspace_root=base)
        handler = FailureHandler(
            snapshot_manager=snapshot_manager,
            planner=planner,
            policy_engine=DefaultPolicyEngine(),
            step_plan_repository=step_plan_repo,
            audit_logger=audit_logger,
            workflow_repository=workflow_repo,
            max_replans=1,
        )

        decision = handler.handle_feedback(
            workflow=workflow,
            step_plan=step_plan,
            state=AgentState(run_id="run-review"),
            review_target="write_summary",
            feedback="Review the original workflow doc and fix the summary write logic.",
            snapshot_ref=None,
        )

        assert decision.should_replan is True
        replanned_workflow = planner.workflows[-1]
        assert replanned_workflow.version == 2
        assert replanned_workflow.replan_context is not None
        assert replanned_workflow.replan_context.trigger == "feedback"
        assert (
            replanned_workflow.replan_context.change_summary
            == "User feedback for target 'write_summary': "
            "Review the original workflow doc and fix the summary write logic."
        )
        assert '<workflow id="wf-review" version="1">' in (
            replanned_workflow.replan_context.source_workflow_document
        )
        assert '"step_plan_id":' in replanned_workflow.replan_context.source_step_plan_document
        stored = workflow_repo.get("wf-review", version=2)
        assert stored is not None
        assert stored.replan_context == replanned_workflow.replan_context
    finally:
        shutil.rmtree(base, ignore_errors=True)
