from __future__ import annotations

from copy import deepcopy

from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.step_plan_repository import IStepPlanRepository
from orchestra_agent.ports.workflow_repository import IWorkflowRepository


class InMemoryWorkflowRepository(IWorkflowRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Workflow] = {}
        self._locked_workflows: set[str] = set()

    def save(self, workflow: Workflow) -> None:
        if workflow.workflow_id in self._locked_workflows:
            raise PermissionError(f"Workflow '{workflow.workflow_id}' is locked.")
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

    def lock_workflow(self, workflow_id: str) -> None:
        self._locked_workflows.add(workflow_id)

    def is_locked(self, workflow_id: str) -> bool:
        return workflow_id in self._locked_workflows


class InMemoryStepPlanRepository(IStepPlanRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], StepPlan] = {}
        self._locked_step_plans: set[tuple[str, str]] = set()

    def save(self, step_plan: StepPlan) -> None:
        key = (step_plan.workflow_id, step_plan.step_plan_id)
        if key in self._locked_step_plans:
            raise PermissionError(f"StepPlan '{step_plan.step_plan_id}' is locked.")
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

    def lock_step_plan(self, workflow_id: str, step_plan_id: str) -> None:
        self._locked_step_plans.add((workflow_id, step_plan_id))

    def is_locked(self, workflow_id: str, step_plan_id: str) -> bool:
        return (workflow_id, step_plan_id) in self._locked_step_plans
