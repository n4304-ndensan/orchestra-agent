from __future__ import annotations

from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.observability import bind_observation_context
from orchestra_agent.ports import (
    IAuditLogger,
    IPlanner,
    IPolicyEngine,
    IStepPlanRepository,
    PolicyEvaluationResult,
)
from orchestra_agent.shared.tool_input_normalization import normalize_step_plan_inputs


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
        with bind_observation_context(
            phase="compile_step_plan",
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
        ):
            plan = self._planner.compile_step_plan(workflow)
            policy_result = self._policy_engine.evaluate(plan)
            normalize_step_plan_inputs(policy_result.step_plan)
            self._step_plan_repository.save(policy_result.step_plan)
        step_plan_path: str | None = None
        step_plan_path_getter = getattr(self._step_plan_repository, "step_plan_json_path", None)
        if callable(step_plan_path_getter):
            step_plan_path = str(
                step_plan_path_getter(
                    workflow.workflow_id,
                    policy_result.step_plan.step_plan_id,
                    policy_result.step_plan.version,
                )
            )
        self._audit_logger.record(
            {
                "event_type": "step_plan_compiled",
                "workflow_id": workflow.workflow_id,
                "workflow_version": workflow.version,
                "step_plan_id": policy_result.step_plan.step_plan_id,
                "step_plan_version": policy_result.step_plan.version,
                "approval_status": policy_result.approval_status.value,
                "step_plan_path": step_plan_path,
            }
        )
        return policy_result

    def compile_only(self, workflow: Workflow) -> StepPlan:
        return normalize_step_plan_inputs(self._planner.compile_step_plan(workflow))
