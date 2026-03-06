from __future__ import annotations

from dataclasses import replace

from orchestra_agent.domain import ApprovalStatus, StepPlan
from orchestra_agent.ports import IAuditLogger, IStepPlanRepository


class ApproveStepPlanUseCase:
    def __init__(
        self,
        step_plan_repository: IStepPlanRepository,
        audit_logger: IAuditLogger,
    ) -> None:
        self._step_plan_repository = step_plan_repository
        self._audit_logger = audit_logger

    def execute(
        self,
        step_plan: StepPlan,
        run_flags: dict[str, bool] | None = None,
        skip_flags: dict[str, bool] | None = None,
        reject: bool = False,
    ) -> tuple[StepPlan, ApprovalStatus]:
        if reject:
            self._audit_logger.record(
                {
                    "event_type": "step_plan_rejected",
                    "step_plan_id": step_plan.step_plan_id,
                    "step_plan_version": step_plan.version,
                }
            )
            return step_plan, ApprovalStatus.REJECTED

        run_flags = run_flags or {}
        skip_flags = skip_flags or {}

        updated_steps = []
        for step in step_plan.steps:
            run = run_flags.get(step.step_id, step.run)
            skip = skip_flags.get(step.step_id, step.skip)
            if not run:
                skip = True
            updated_steps.append(replace(step, run=run, skip=skip))

        approved_plan = StepPlan(
            step_plan_id=step_plan.step_plan_id,
            workflow_id=step_plan.workflow_id,
            version=step_plan.version + 1,
            steps=updated_steps,
        )
        self._step_plan_repository.save(approved_plan)

        self._audit_logger.record(
            {
                "event_type": "step_plan_approved",
                "step_plan_id": approved_plan.step_plan_id,
                "step_plan_version": approved_plan.version,
                "edited_steps": sorted(set(run_flags.keys()) | set(skip_flags.keys())),
            }
        )
        return approved_plan, ApprovalStatus.APPROVED
