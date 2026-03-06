from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestra_agent.domain.enums import ApprovalStatus
from orchestra_agent.domain.step_plan import StepPlan


@dataclass(slots=True)
class PolicyEvaluationResult:
    step_plan: StepPlan
    approval_status: ApprovalStatus
    reasons: list[str]


class IPolicyEngine(Protocol):
    def evaluate(self, step_plan: StepPlan) -> PolicyEvaluationResult:
        ...

