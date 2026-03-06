from __future__ import annotations

from typing import Protocol

from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow


class IPlanner(Protocol):
    def compile_step_plan(self, workflow: Workflow) -> StepPlan:
        ...

