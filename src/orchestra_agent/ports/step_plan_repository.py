from __future__ import annotations

from typing import Protocol

from orchestra_agent.domain.step_plan import StepPlan


class IStepPlanRepository(Protocol):
    def save(self, step_plan: StepPlan) -> None:
        ...

    def get(self, step_plan_id: str, version: int | None = None) -> StepPlan | None:
        ...

