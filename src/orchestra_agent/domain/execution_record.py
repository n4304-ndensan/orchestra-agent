from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchestra_agent.domain.enums import ExecutionStatus


@dataclass(slots=True)
class ExecutionRecord:
    step_id: str
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    snapshot_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def pending(cls, step_id: str) -> "ExecutionRecord":
        now = datetime.now(UTC)
        return cls(step_id=step_id, status=ExecutionStatus.PENDING, started_at=now)

    def mark_running(self) -> None:
        self.status = ExecutionStatus.RUNNING
        self.started_at = datetime.now(UTC)

    def mark_success(self, result: dict[str, Any] | None = None) -> None:
        self.status = ExecutionStatus.SUCCESS
        self.result = result
        self.finished_at = datetime.now(UTC)

    def mark_failed(self, error: str) -> None:
        self.status = ExecutionStatus.FAILED
        self.error = error
        self.finished_at = datetime.now(UTC)

    def mark_skipped(self) -> None:
        self.status = ExecutionStatus.SKIPPED
        now = datetime.now(UTC)
        self.started_at = now
        self.finished_at = now

