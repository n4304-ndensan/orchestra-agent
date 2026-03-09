from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestra_agent.domain.enums import BackupScope, RiskLevel
from orchestra_agent.domain.errors import DomainValidationError


@dataclass(slots=True)
class Step:
    step_id: str
    name: str
    description: str
    tool_ref: str
    resolved_input: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    run: bool = True
    skip: bool = False
    backup_scope: BackupScope = BackupScope.NONE

    def __post_init__(self) -> None:
        if self.run and self.skip:
            raise DomainValidationError(
                f"Step '{self.step_id}' has invalid flags: run=True and skip=True."
            )
        if not self.tool_ref.strip():
            raise DomainValidationError(f"Step '{self.step_id}' must include a tool_ref.")

    @property
    def is_executable(self) -> bool:
        return self.run and not self.skip

    @property
    def requires_runtime_approval(self) -> bool:
        return self.requires_approval or self.risk_level in (
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        )

