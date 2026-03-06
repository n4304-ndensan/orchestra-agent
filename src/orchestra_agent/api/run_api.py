from __future__ import annotations

from typing import Any

from orchestra_agent.application.use_cases import ExecutePlanUseCase
from orchestra_agent.domain import AgentState, ApprovalStatus
from orchestra_agent.ports import IAgentStateStore, IStepPlanRepository, IWorkflowRepository


class RunAPI:
    def __init__(
        self,
        execute_plan_use_case: ExecutePlanUseCase,
        workflow_repository: IWorkflowRepository,
        step_plan_repository: IStepPlanRepository,
        state_store: IAgentStateStore,
    ) -> None:
        self._execute_plan_use_case = execute_plan_use_case
        self._workflow_repository = workflow_repository
        self._step_plan_repository = step_plan_repository
        self._state_store = state_store

    def start_run(
        self,
        workflow_id: str,
        step_plan_id: str,
        run_id: str | None = None,
        approved: bool = False,
    ) -> dict[str, Any]:
        workflow = self._workflow_repository.get(workflow_id)
        if workflow is None:
            raise KeyError(f"Workflow '{workflow_id}' not found.")
        step_plan = self._step_plan_repository.get(step_plan_id)
        if step_plan is None:
            raise KeyError(f"StepPlan '{step_plan_id}' not found.")

        state = self._execute_plan_use_case.execute(
            workflow=workflow,
            step_plan=step_plan,
            run_id=run_id,
            approval_status=ApprovalStatus.APPROVED if approved else ApprovalStatus.PENDING,
        )
        return self._serialize_state(state)

    def get_run(self, run_id: str) -> dict[str, Any]:
        state = self._state_store.load(run_id)
        if state is None:
            raise KeyError(f"Run '{run_id}' not found.")
        return self._serialize_state(state)

    def resume_run(self, run_id: str, approved: bool = True) -> dict[str, Any]:
        state = self._state_store.load(run_id)
        if state is None:
            raise KeyError(f"Run '{run_id}' not found.")
        if state.workflow_id is None:
            raise ValueError("Run does not have workflow_id.")
        if state.step_plan_id is None:
            raise ValueError("Run does not have step_plan_id.")

        workflow = self._workflow_repository.get(state.workflow_id, version=state.workflow_version)
        if workflow is None:
            raise KeyError(f"Workflow '{state.workflow_id}' not found.")
        step_plan = self._step_plan_repository.get(
            state.step_plan_id,
            version=state.step_plan_version,
        )
        if step_plan is None:
            raise KeyError(f"StepPlan '{state.step_plan_id}' not found.")

        approval_status = ApprovalStatus.APPROVED if approved else state.approval_status
        resumed = self._execute_plan_use_case.execute(
            workflow=workflow,
            step_plan=step_plan,
            run_id=run_id,
            approval_status=approval_status,
        )
        return self._serialize_state(resumed)

    @staticmethod
    def _serialize_state(state: AgentState) -> dict[str, Any]:
        return {
            "run_id": state.run_id,
            "workflow_id": state.workflow_id,
            "workflow_version": state.workflow_version,
            "step_plan_id": state.step_plan_id,
            "step_plan_version": state.step_plan_version,
            "current_step_id": state.current_step_id,
            "approval_status": state.approval_status.value,
            "execution_history": [
                {
                    "step_id": record.step_id,
                    "status": record.status.value,
                    "started_at": record.started_at.isoformat(),
                    "finished_at": record.finished_at.isoformat() if record.finished_at else None,
                    "result": record.result,
                    "error": record.error,
                    "snapshot_ref": record.snapshot_ref,
                    "metadata": record.metadata,
                }
                for record in state.execution_history
            ],
            "snapshot_refs": state.snapshot_refs,
            "last_error": state.last_error,
            "metadata": state.metadata,
        }
