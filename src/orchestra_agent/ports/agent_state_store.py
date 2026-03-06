from __future__ import annotations

from typing import Protocol

from orchestra_agent.domain.agent_state import AgentState
from orchestra_agent.domain.execution_record import ExecutionRecord


class IAgentStateStore(Protocol):
    def load(self, run_id: str) -> AgentState | None:
        ...

    def save(self, state: AgentState) -> None:
        ...

    def append_execution(self, run_id: str, record: ExecutionRecord) -> None:
        ...

