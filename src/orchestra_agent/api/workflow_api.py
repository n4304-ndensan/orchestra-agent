from __future__ import annotations

from typing import Any

from orchestra_agent.application.use_cases import CompileStepPlanUseCase, CreateWorkflowUseCase
from orchestra_agent.domain.serialization import step_plan_to_dict, workflow_to_dict
from orchestra_agent.ports import IWorkflowRepository


class WorkflowAPI:
    def __init__(
        self,
        create_workflow_use_case: CreateWorkflowUseCase,
        compile_step_plan_use_case: CompileStepPlanUseCase,
        workflow_repository: IWorkflowRepository,
    ) -> None:
        self._create_workflow_use_case = create_workflow_use_case
        self._compile_step_plan_use_case = compile_step_plan_use_case
        self._workflow_repository = workflow_repository

    def create_workflow(
        self,
        name: str,
        objective: str,
        reference_files: list[str] | None = None,
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = self._create_workflow_use_case.execute(
            name=name,
            objective=objective,
            reference_files=reference_files,
            constraints=constraints,
            success_criteria=success_criteria,
            workflow_id=workflow_id,
        )
        return workflow_to_dict(workflow)

    def generate_step_plan(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._workflow_repository.get(workflow_id)
        if workflow is None:
            raise KeyError(f"Workflow '{workflow_id}' not found.")

        result = self._compile_step_plan_use_case.execute(workflow)
        return {
            **step_plan_to_dict(result.step_plan),
            "approval_status": result.approval_status.value,
            "reasons": result.reasons,
        }
