from __future__ import annotations

from orchestra_agent.domain import AgentState, ApprovalStatus, StepPlan, Workflow
from orchestra_agent.executor import PlanExecutor
from orchestra_agent.ports import IAgentStateStore, IAuditLogger


class ExecutePlanUseCase:
    def __init__(
        self,
        executor: PlanExecutor,
        state_store: IAgentStateStore,
        audit_logger: IAuditLogger,
    ) -> None:
        self._executor = executor
        self._state_store = state_store
        self._audit_logger = audit_logger

    def execute(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        run_id: str | None = None,
        approval_status: ApprovalStatus = ApprovalStatus.PENDING,
    ) -> AgentState:
        if run_id is not None:
            state = self._state_store.load(run_id) or AgentState(run_id=run_id)
        else:
            state = AgentState()

        state.workflow_id = workflow.workflow_id
        state.workflow_version = workflow.version
        state.step_plan_id = step_plan.step_plan_id
        state.step_plan_version = step_plan.version
        state.approval_status = approval_status
        self._state_store.save(state)

        self._audit_logger.record(
            {
                "event_type": "run_started",
                "run_id": state.run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "step_plan_version": step_plan.version,
                "approval_status": approval_status.value,
            }
        )
        return self._executor.execute(workflow=workflow, step_plan=step_plan, state=state)

    def apply_feedback(
        self,
        workflow: Workflow,
        step_plan: StepPlan,
        run_id: str,
        feedback: str,
    ) -> AgentState:
        state = self._state_store.load(run_id)
        if state is None:
            raise KeyError(f"Run '{run_id}' not found.")

        self._audit_logger.record(
            {
                "event_type": "run_feedback_submitted",
                "run_id": run_id,
                "workflow_id": workflow.workflow_id,
                "step_plan_id": step_plan.step_plan_id,
                "feedback": feedback,
            }
        )
        return self._executor.submit_feedback(
            workflow=workflow,
            step_plan=step_plan,
            state=state,
            feedback=feedback,
        )
