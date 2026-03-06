from __future__ import annotations

from copy import deepcopy

from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.step_plan_repository import IStepPlanRepository
from orchestra_agent.ports.workflow_repository import IWorkflowRepository


class InMemoryWorkflowRepository(IWorkflowRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Workflow] = {}

    def save(self, workflow: Workflow) -> None:
        self._items[(workflow.workflow_id, workflow.version)] = deepcopy(workflow)

    def get(self, workflow_id: str, version: int | None = None) -> Workflow | None:
        if version is not None:
            item = self._items.get((workflow_id, version))
            return deepcopy(item) if item is not None else None

        candidates = [w for (wid, _), w in self._items.items() if wid == workflow_id]
        if not candidates:
            return None
        latest = max(candidates, key=lambda workflow: workflow.version)
        return deepcopy(latest)


class InMemoryStepPlanRepository(IStepPlanRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], StepPlan] = {}

    def save(self, step_plan: StepPlan) -> None:
        self._items[(step_plan.step_plan_id, step_plan.version)] = deepcopy(step_plan)

    def get(self, step_plan_id: str, version: int | None = None) -> StepPlan | None:
        if version is not None:
            item = self._items.get((step_plan_id, version))
            return deepcopy(item) if item is not None else None

        candidates = [p for (sid, _), p in self._items.items() if sid == step_plan_id]
        if not candidates:
            return None
        latest = max(candidates, key=lambda step_plan: step_plan.version)
        return deepcopy(latest)

