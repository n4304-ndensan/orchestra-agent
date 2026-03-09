from __future__ import annotations

from uuid import uuid4

from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports import IAuditLogger, IWorkflowRepository


class CreateWorkflowUseCase:
    def __init__(
        self,
        workflow_repository: IWorkflowRepository,
        audit_logger: IAuditLogger,
    ) -> None:
        self._workflow_repository = workflow_repository
        self._audit_logger = audit_logger

    def execute(
        self,
        name: str,
        objective: str,
        reference_files: list[str] | None = None,
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        workflow_id: str | None = None,
    ) -> Workflow:
        new_workflow = Workflow(
            workflow_id=workflow_id or f"wf-{uuid4().hex[:10]}",
            name=name,
            version=1,
            objective=objective,
            reference_files=reference_files or [],
            constraints=constraints or [],
            success_criteria=success_criteria or [],
            feedback_history=[],
        )
        self._workflow_repository.save(new_workflow)
        self._audit_logger.record(
            {
                "event_type": "workflow_created",
                "workflow_id": new_workflow.workflow_id,
                "workflow_version": new_workflow.version,
            }
        )
        return new_workflow
