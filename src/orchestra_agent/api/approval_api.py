from __future__ import annotations

from typing import Any

from orchestra_agent.application.use_cases import ApproveStepPlanUseCase
from orchestra_agent.ports import IStepPlanRepository


class ApprovalAPI:
    def __init__(
        self,
        approve_step_plan_use_case: ApproveStepPlanUseCase,
        step_plan_repository: IStepPlanRepository,
    ) -> None:
        self._approve_step_plan_use_case = approve_step_plan_use_case
        self._step_plan_repository = step_plan_repository

    def approve_step_plan(
        self,
        step_plan_id: str,
        run_flags: dict[str, bool] | None = None,
        skip_flags: dict[str, bool] | None = None,
        reject: bool = False,
    ) -> dict[str, Any]:
        step_plan = self._step_plan_repository.get(step_plan_id)
        if step_plan is None:
            raise KeyError(f"StepPlan '{step_plan_id}' not found.")

        approved_plan, approval_status = self._approve_step_plan_use_case.execute(
            step_plan=step_plan,
            run_flags=run_flags,
            skip_flags=skip_flags,
            reject=reject,
        )
        return {
            "step_plan_id": approved_plan.step_plan_id,
            "version": approved_plan.version,
            "approval_status": approval_status.value,
            "steps": [
                {
                    "step_id": step.step_id,
                    "run": step.run,
                    "skip": step.skip,
                }
                for step in approved_plan.steps
            ],
        }

