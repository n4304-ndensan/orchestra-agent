from __future__ import annotations

from typing import Any

from orchestra_agent.application.use_cases import CompileStepPlanUseCase, CreateWorkflowUseCase
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
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        workflow = self._create_workflow_use_case.execute(
            name=name,
            objective=objective,
            constraints=constraints,
            success_criteria=success_criteria,
        )
        return {
            "workflow_id": workflow.workflow_id,
            "name": workflow.name,
            "version": workflow.version,
            "objective": workflow.objective,
            "constraints": workflow.constraints,
            "success_criteria": workflow.success_criteria,
            "feedback_history": workflow.feedback_history,
        }

    def generate_step_plan(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._workflow_repository.get(workflow_id)
        if workflow is None:
            raise KeyError(f"Workflow '{workflow_id}' not found.")

        result = self._compile_step_plan_use_case.execute(workflow)
        return {
            "step_plan_id": result.step_plan.step_plan_id,
            "workflow_id": result.step_plan.workflow_id,
            "version": result.step_plan.version,
            "approval_status": result.approval_status.value,
            "reasons": result.reasons,
            "steps": [
                {
                    "step_id": step.step_id,
                    "name": step.name,
                    "description": step.description,
                    "tool_ref": step.tool_ref,
                    "depends_on": step.depends_on,
                    "risk_level": step.risk_level.value,
                    "requires_approval": step.requires_approval,
                    "run": step.run,
                    "skip": step.skip,
                    "backup_scope": step.backup_scope.value,
                    "resolved_input": step.resolved_input,
                }
                for step in result.step_plan.steps
            ],
        }

