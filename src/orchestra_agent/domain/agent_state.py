from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from orchestra_agent.domain.enums import ApprovalStatus
from orchestra_agent.domain.execution_record import ExecutionRecord


@dataclass(slots=True)
class AgentState:
    run_id: str = field(default_factory=lambda: str(uuid4()))
    workflow_id: str | None = None
    workflow_version: int | None = None
    step_plan_id: str | None = None
    step_plan_version: int | None = None
    current_step_id: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    execution_history: list[ExecutionRecord] = field(default_factory=list)
    snapshot_refs: list[str] = field(default_factory=list)
    last_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def append_execution(self, record: ExecutionRecord) -> None:
        self.execution_history.append(record)
        self.current_step_id = record.step_id
        if record.error is not None:
            self.last_error = record.error

