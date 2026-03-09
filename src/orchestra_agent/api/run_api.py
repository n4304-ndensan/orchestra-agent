from __future__ import annotations

from typing import Any, cast

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
        self._lock_completed_artifacts(state)
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
        self._lock_completed_artifacts(resumed)
        return self._serialize_state(resumed)

    def respond_to_approval(
        self,
        run_id: str,
        approve: bool = True,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        state = self._state_store.load(run_id)
        if state is None:
            raise KeyError(f"Run '{run_id}' not found.")

        if feedback is not None:
            return self.submit_feedback(run_id=run_id, feedback=feedback)

        if approve:
            return self.resume_run(run_id=run_id, approved=True)

        state.approval_status = ApprovalStatus.REJECTED
        self._state_store.save(state)
        return self._serialize_state(state)

    def submit_feedback(self, run_id: str, feedback: str) -> dict[str, Any]:
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

        updated = self._execute_plan_use_case.apply_feedback(
            workflow=workflow,
            step_plan=step_plan,
            run_id=run_id,
            feedback=feedback,
        )
        return self._serialize_state(updated)

    @staticmethod
    def _serialize_state(state: AgentState) -> dict[str, Any]:
        pending_approval: dict[str, Any] | None = None
        approval_context = state.metadata.get("approval_context")
        if isinstance(approval_context, dict):
            stage = approval_context.get("stage")
            step_id = approval_context.get("step_id")
            message = approval_context.get("message")
            if isinstance(stage, str) and isinstance(step_id, str) and isinstance(message, str):
                pending_approval = {
                    "stage": stage,
                    "step_id": step_id,
                    "message": message,
                }

        return {
            "run_id": state.run_id,
            "workflow_id": state.workflow_id,
            "workflow_version": state.workflow_version,
            "step_plan_id": state.step_plan_id,
            "step_plan_version": state.step_plan_version,
            "current_step_id": state.current_step_id,
            "approval_status": state.approval_status.value,
            "pending_approval": pending_approval,
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

    def _lock_completed_artifacts(self, state: AgentState) -> None:
        if state.workflow_id is None or state.step_plan_id is None:
            return
        if state.current_step_id is not None:
            return
        if state.last_error is not None:
            return
        if state.approval_status != ApprovalStatus.APPROVED:
            return
        approval_context = state.metadata.get("approval_context")
        if isinstance(approval_context, dict):
            return

        workflow_repo = cast(Any, self._workflow_repository)
        if hasattr(workflow_repo, "lock_workflow"):
            workflow_repo.lock_workflow(state.workflow_id)

        step_plan_repo = cast(Any, self._step_plan_repository)
        if hasattr(step_plan_repo, "lock_step_plan"):
            step_plan_repo.lock_step_plan(state.workflow_id, state.step_plan_id)

        state.metadata["artifacts_locked"] = True
        self._state_store.save(state)
