from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Workflow:
    workflow_id: str
    name: str
    version: int
    objective: str
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    feedback_history: list[str] = field(default_factory=list)

    def with_feedback(self, feedback: str) -> "Workflow":
        new_history = [*self.feedback_history, feedback]
        return Workflow(
            workflow_id=self.workflow_id,
            name=self.name,
            version=self.version + 1,
            objective=self.objective,
            constraints=[*self.constraints],
            success_criteria=[*self.success_criteria],
            feedback_history=new_history,
        )

