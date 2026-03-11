from __future__ import annotations

from importlib import import_module
from typing import Any

from orchestra_agent.adapters.db import (
    FilesystemAgentStateStore,
    FilesystemAuditLogger,
    FilesystemStepPlanRepository,
    InMemoryAuditLogger,
    InMemoryStepPlanRepository,
    InMemoryWorkflowRepository,
    PostgresAgentStateStore,
    XmlWorkflowRepository,
)
from orchestra_agent.adapters.execution import LlmStepExecutor
from orchestra_agent.adapters.mcp import (
    JsonRpcMcpClient,
    MockExcelMcpClient,
    MultiEndpointMcpClient,
)
from orchestra_agent.adapters.planner import (
    JsonFileStepProposalProvider,
    LlmPlanner,
    LlmStepProposalProvider,
    PlannerDefaults,
    SafeAugmentedLlmPlanner,
    StructuredLlmPlanner,
)
from orchestra_agent.adapters.policy import DefaultPolicyEngine
from orchestra_agent.adapters.snapshot import FilesystemSnapshotManager

__all__ = [
    "DefaultPolicyEngine",
    "FilesystemAgentStateStore",
    "FilesystemAuditLogger",
    "FilesystemStepPlanRepository",
    "FilesystemSnapshotManager",
    "ChatGptPlaywrightLlmClient",
    "GoogleGeminiLlmClient",
    "InMemoryAuditLogger",
    "InMemoryStepPlanRepository",
    "InMemoryWorkflowRepository",
    "JsonFileStepProposalProvider",
    "JsonRpcMcpClient",
    "LlmStepExecutor",
    "LlmStepProposalProvider",
    "MockExcelMcpClient",
    "MultiEndpointMcpClient",
    "LlmPlanner",
    "OpenAILlmClient",
    "PlannerDefaults",
    "PostgresAgentStateStore",
    "SafeAugmentedLlmPlanner",
    "StructuredLlmPlanner",
    "XmlWorkflowRepository",
]


def __getattr__(name: str) -> Any:
    if name in {"ChatGptPlaywrightLlmClient", "GoogleGeminiLlmClient", "OpenAILlmClient"}:
        module = import_module("orchestra_agent.adapters.llm")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
