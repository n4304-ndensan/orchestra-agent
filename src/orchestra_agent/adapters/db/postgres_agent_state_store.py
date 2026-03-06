from __future__ import annotations

from copy import deepcopy

from orchestra_agent.domain.agent_state import AgentState
from orchestra_agent.domain.execution_record import ExecutionRecord
from orchestra_agent.ports.agent_state_store import IAgentStateStore


class PostgresAgentStateStore(IAgentStateStore):
    """
    In-memory implementation that follows the AgentStateStore port contract.
    The class name is kept for architecture compatibility and can be backed by
    a real database adapter later.
    """

    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}

    def load(self, run_id: str) -> AgentState | None:
        state = self._states.get(run_id)
        if state is None:
            return None
        return deepcopy(state)

    def save(self, state: AgentState) -> None:
        self._states[state.run_id] = deepcopy(state)

    def append_execution(self, run_id: str, record: ExecutionRecord) -> None:
        state = self._states.get(run_id)
        if state is None:
            raise KeyError(f"run_id '{run_id}' not found.")
        state.append_execution(record)
        self._states[run_id] = deepcopy(state)

