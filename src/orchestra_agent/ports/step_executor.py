from __future__ import annotations

from typing import Any, Protocol

from orchestra_agent.domain.step import Step
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.mcp_client import IMcpClient


class IStepExecutor(Protocol):
    def execute(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        mcp_client: IMcpClient,
    ) -> dict[str, Any]:
        ...
