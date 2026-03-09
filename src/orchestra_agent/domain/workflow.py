from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ReplanContext:
    trigger: str
    change_summary: str
    source_workflow_document: str
    source_step_plan_document: str


@dataclass(slots=True)
class Workflow:
    workflow_id: str
    name: str
    version: int
    objective: str
    reference_files: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    feedback_history: list[str] = field(default_factory=list)
    replan_context: ReplanContext | None = None

    def with_feedback(
        self,
        feedback: str,
        replan_context: ReplanContext | None = None,
    ) -> Workflow:
        new_history = [*self.feedback_history, feedback]
        return Workflow(
            workflow_id=self.workflow_id,
            name=self.name,
            version=self.version + 1,
            objective=self.objective,
            reference_files=[*self.reference_files],
            constraints=[*self.constraints],
            success_criteria=[*self.success_criteria],
            feedback_history=new_history,
            replan_context=replan_context,
        )
