from __future__ import annotations

from typing import Protocol

from orchestra_agent.domain.workflow import Workflow


class IWorkflowRepository(Protocol):
    def save(self, workflow: Workflow) -> None:
        ...

    def get(self, workflow_id: str, version: int | None = None) -> Workflow | None:
        ...

