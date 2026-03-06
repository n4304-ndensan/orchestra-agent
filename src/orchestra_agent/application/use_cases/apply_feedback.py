from __future__ import annotations

from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports import IAuditLogger, IWorkflowRepository


class ApplyFeedbackUseCase:
    def __init__(
        self,
        workflow_repository: IWorkflowRepository,
        audit_logger: IAuditLogger,
    ) -> None:
        self._workflow_repository = workflow_repository
        self._audit_logger = audit_logger

    def execute(self, workflow_id: str, feedback: str) -> Workflow:
        existing = self._workflow_repository.get(workflow_id)
        if existing is None:
            raise KeyError(f"Workflow '{workflow_id}' not found.")

        updated = existing.with_feedback(feedback)
        self._workflow_repository.save(updated)
        self._audit_logger.record(
            {
                "event_type": "workflow_feedback_applied",
                "workflow_id": workflow_id,
                "workflow_version": updated.version,
            }
        )
        return updated
