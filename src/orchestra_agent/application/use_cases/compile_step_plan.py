from __future__ import annotations

from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports import (
    IAuditLogger,
    IPlanner,
    IPolicyEngine,
    IStepPlanRepository,
    PolicyEvaluationResult,
)


class CompileStepPlanUseCase:
    def __init__(
        self,
        planner: IPlanner,
        policy_engine: IPolicyEngine,
        step_plan_repository: IStepPlanRepository,
        audit_logger: IAuditLogger,
    ) -> None:
        self._planner = planner
        self._policy_engine = policy_engine
        self._step_plan_repository = step_plan_repository
        self._audit_logger = audit_logger

    def execute(self, workflow: Workflow) -> PolicyEvaluationResult:
        plan = self._planner.compile_step_plan(workflow)
        policy_result = self._policy_engine.evaluate(plan)
        self._step_plan_repository.save(policy_result.step_plan)
        self._audit_logger.record(
            {
                "event_type": "step_plan_compiled",
                "workflow_id": workflow.workflow_id,
                "workflow_version": workflow.version,
                "step_plan_id": policy_result.step_plan.step_plan_id,
                "step_plan_version": policy_result.step_plan.version,
                "approval_status": policy_result.approval_status.value,
            }
        )
        return policy_result

    def compile_only(self, workflow: Workflow) -> StepPlan:
        return self._planner.compile_step_plan(workflow)

