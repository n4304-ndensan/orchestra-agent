from __future__ import annotations

from dataclasses import dataclass

from orchestra_agent.domain import (
    AgentState,
    ApprovalStatus,
    ReplanContext,
    Step,
    StepPlan,
    Workflow,
)
from orchestra_agent.domain.serialization import step_plan_to_json_text, workflow_to_xml_text
from orchestra_agent.observability import bind_observation_context
from orchestra_agent.ports import (
    IAuditLogger,
    IPlanner,
    IPolicyEngine,
    ISnapshotManager,
    IStepPlanRepository,
    IWorkflowRepository,
)


@dataclass(slots=True)
class FailureContext:
    workflow: Workflow
    step_plan: StepPlan
    state: AgentState
    failed_step: Step
    error_message: str
    snapshot_ref: str | None


@dataclass(slots=True)
class RecoveryDecision:
    should_replan: bool
    workflow: Workflow | None
    step_plan: StepPlan | None
    approval_status: ApprovalStatus
    reason: str


class FailureHandler:
    def __init__(
        self,
        snapshot_manager: ISnapshotManager,
        planner: IPlanner,
        policy_engine: IPolicyEngine,
        step_plan_repository: IStepPlanRepository,
        audit_logger: IAuditLogger,
        workflow_repository: IWorkflowRepository | None = None,
        max_replans: int = 3,
    ) -> None:
        self._snapshot_manager = snapshot_manager
        self._planner = planner
        self._policy_engine = policy_engine
        self._step_plan_repository = step_plan_repository
        self._audit_logger = audit_logger
        self._workflow_repository = workflow_repository
        self._max_replans = max_replans

    def handle_failure(self, context: FailureContext, replan_attempt: int) -> RecoveryDecision:
        if context.snapshot_ref is not None:
            self._snapshot_manager.restore_snapshot(context.snapshot_ref)

        self._audit_logger.record(
            {
                "event_type": "execution_failure",
                "run_id": context.state.run_id,
                "step_id": context.failed_step.step_id,
                "error": context.error_message,
                "replan_attempt": replan_attempt,
            }
        )

        feedback = (
            f"Execution failed at step '{context.failed_step.step_id}' with error: "
            f"{context.error_message}"
        )
        return self._replan_with_feedback(
            workflow=context.workflow,
            previous_plan=context.step_plan,
            run_id=context.state.run_id,
            feedback=feedback,
            replan_attempt=replan_attempt,
            event_type="replanned_step_plan",
            reason="Replanned after failure.",
            trigger="failure",
            enforce_limit=True,
        )

    def handle_feedback(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        state: AgentState,
        review_target: str,
        feedback: str,
        snapshot_ref: str | None,
        replan_attempt: int = 0,
    ) -> RecoveryDecision:
        if snapshot_ref is not None:
            self._snapshot_manager.restore_snapshot(snapshot_ref)

        self._audit_logger.record(
            {
                "event_type": "step_feedback_received",
                "run_id": state.run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "step_id": review_target,
                "feedback": feedback,
            }
        )

        message = f"User feedback for target '{review_target}': {feedback}"
        return self._replan_with_feedback(
            workflow=workflow,
            previous_plan=step_plan,
            run_id=state.run_id,
            feedback=message,
            replan_attempt=replan_attempt,
            event_type="replanned_from_feedback",
            reason="Replanned after user feedback.",
            trigger="feedback",
            enforce_limit=False,
        )

    def _replan_with_feedback(
        self,
        workflow: Workflow,
        previous_plan: StepPlan,
        run_id: str,
        feedback: str,
        replan_attempt: int,
        event_type: str,
        reason: str,
        trigger: str,
        enforce_limit: bool,
    ) -> RecoveryDecision:
        if enforce_limit and replan_attempt >= self._max_replans:
            return RecoveryDecision(
                should_replan=False,
                workflow=None,
                step_plan=None,
                approval_status=ApprovalStatus.REJECTED,
                reason="Replan limit reached.",
            )

        with bind_observation_context(
            phase="replan_with_feedback",
            run_id=run_id,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
            trigger=trigger,
        ):
            updated_workflow = workflow.with_feedback(
                feedback,
                replan_context=self._build_replan_context(
                    workflow=workflow,
                    previous_plan=previous_plan,
                    trigger=trigger,
                    change_summary=feedback,
                ),
            )
            if self._workflow_repository is not None:
                self._workflow_repository.save(updated_workflow)

            replanned = self._planner.compile_step_plan(updated_workflow)
            replanned = self._carry_forward_paths(previous_plan, replanned)
            evaluated = self._policy_engine.evaluate(replanned)
            self._step_plan_repository.save(evaluated.step_plan)

        workflow_path: str | None = None
        workflow_path_getter = getattr(self._workflow_repository, "workflow_path", None)
        if callable(workflow_path_getter):
            workflow_path = str(
                workflow_path_getter(updated_workflow.workflow_id, updated_workflow.version)
            )
        step_plan_path: str | None = None
        step_plan_path_getter = getattr(self._step_plan_repository, "step_plan_json_path", None)
        if callable(step_plan_path_getter):
            step_plan_path = str(
                step_plan_path_getter(
                    updated_workflow.workflow_id,
                    evaluated.step_plan.step_plan_id,
                    evaluated.step_plan.version,
                )
            )
        self._audit_logger.record(
            {
                "event_type": event_type,
                "run_id": run_id,
                "workflow_id": updated_workflow.workflow_id,
                "workflow_version": updated_workflow.version,
                "step_plan_id": evaluated.step_plan.step_plan_id,
                "step_plan_version": evaluated.step_plan.version,
                "approval_status": evaluated.approval_status.value,
                "workflow_path": workflow_path,
                "step_plan_path": step_plan_path,
            }
        )

        return RecoveryDecision(
            should_replan=True,
            workflow=updated_workflow,
            step_plan=evaluated.step_plan,
            approval_status=evaluated.approval_status,
            reason=reason,
        )

    @staticmethod
    def _carry_forward_paths(previous_plan: StepPlan, replanned: StepPlan) -> StepPlan:
        previous_steps = previous_plan.step_map()
        for step in replanned.steps:
            previous = previous_steps.get(step.step_id)
            if previous is None:
                continue
            for key in ("file", "output"):
                previous_value = previous.resolved_input.get(key)
                if isinstance(previous_value, str):
                    step.resolved_input[key] = previous_value
        return replanned

    @staticmethod
    def _build_replan_context(
        workflow: Workflow,
        previous_plan: StepPlan,
        trigger: str,
        change_summary: str,
    ) -> ReplanContext:
        return ReplanContext(
            trigger=trigger,
            change_summary=change_summary,
            source_workflow_document=workflow_to_xml_text(workflow),
            source_step_plan_document=step_plan_to_json_text(previous_plan),
        )
