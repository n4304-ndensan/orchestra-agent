from __future__ import annotations

from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.ports import IPolicyEngine, PolicyEvaluationResult


class EvaluatePolicyUseCase:
    def __init__(self, policy_engine: IPolicyEngine) -> None:
        self._policy_engine = policy_engine

    def execute(self, step_plan: StepPlan) -> PolicyEvaluationResult:
        return self._policy_engine.evaluate(step_plan)

