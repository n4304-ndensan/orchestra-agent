from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from orchestra_agent.domain.errors import DomainValidationError
from orchestra_agent.domain.step import Step


@dataclass(slots=True)
class StepPlan:
    step_plan_id: str
    workflow_id: str
    version: int
    steps: list[Step] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        ids = [step.step_id for step in self.steps]
        unique_ids = set(ids)
        if len(ids) != len(unique_ids):
            raise DomainValidationError("Step IDs in StepPlan must be unique.")

        id_set = set(ids)
        for step in self.steps:
            missing = [dep for dep in step.depends_on if dep not in id_set]
            if missing:
                raise DomainValidationError(
                    f"Step '{step.step_id}' has unknown dependencies: {missing}."
                )

        _ = self.topologically_sorted_ids()

    def step_map(self) -> dict[str, Step]:
        return {step.step_id: step for step in self.steps}

    def topologically_sorted_ids(self) -> list[str]:
        adjacency: dict[str, set[str]] = {step.step_id: set() for step in self.steps}
        in_degree: dict[str, int] = {step.step_id: 0 for step in self.steps}

        for step in self.steps:
            for dependency in step.depends_on:
                adjacency[dependency].add(step.step_id)
                in_degree[step.step_id] += 1

        queue: deque[str] = deque(sorted([k for k, v in in_degree.items() if v == 0]))
        ordered: list[str] = []

        while queue:
            node = queue.popleft()
            ordered.append(node)
            for child in sorted(adjacency[node]):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(ordered) != len(self.steps):
            raise DomainValidationError("Step dependencies must form a DAG.")
        return ordered

    def ordered_steps(self) -> list[Step]:
        mapped = self.step_map()
        return [mapped[step_id] for step_id in self.topologically_sorted_ids()]

