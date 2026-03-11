from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestra_agent.adapters.db import (
    FilesystemAuditLogger,
    FilesystemStepPlanRepository,
    XmlWorkflowRepository,
)
from orchestra_agent.api import ApprovalAPI, RunAPI, WorkflowAPI
from orchestra_agent.ports import ILlmClient, IMcpClient, IPlanner
from orchestra_agent.shared.llm_prompting import LlmLanguage

type LlmProviderName = str
type PlannerMode = Literal["deterministic", "augmented", "full"]


@dataclass(slots=True, frozen=True)
class RuntimeMetadata:
    app_version: str
    llm_provider: LlmProviderName
    planner_mode: PlannerMode
    llm_language: LlmLanguage
    llm_remembers_context: bool
    mcp_endpoints: tuple[str, ...]


@dataclass(slots=True)
class RuntimeConfig:
    workspace: Path
    snapshots_dir: Path
    workflow_root: Path
    plan_root: Path
    state_root: Path
    audit_root: Path
    mcp_endpoint: str | None = None
    mcp_endpoints: tuple[str, ...] = ()
    llm_provider: LlmProviderName = "none"
    llm_provider_modules: tuple[str, ...] = ()
    llm_proposal_file: str | None = None
    llm_openai_model: str = "gpt-4.1-mini"
    llm_openai_api_key_env: str = "OPENAI_API_KEY"
    llm_openai_base_url: str = "https://api.openai.com"
    llm_openai_timeout: float = 60.0
    llm_google_model: str = "gemini-2.5-flash"
    llm_google_api_key_env: str = "GEMINI_API_KEY"
    llm_google_base_url: str = "https://generativelanguage.googleapis.com"
    llm_google_timeout: float = 60.0
    llm_chatgpt_url: str = "https://chatgpt.com/ja-JP/"
    llm_chatgpt_chrome_path: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    llm_chatgpt_profile_dir: Path | None = None
    llm_chatgpt_port: int = 9222
    llm_tls_verify: bool = True
    llm_tls_ca_bundle: Path | None = None
    llm_planner_mode: PlannerMode | None = None
    llm_language: LlmLanguage = "en"
    llm_remembers_context: bool = False
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1200
    repair_max_attempts: int = 3


@dataclass(slots=True)
class RuntimeArtifacts:
    workspace_root: Path
    workflow_root: Path
    plan_root: Path
    snapshots_dir: Path
    state_root: Path
    audit_root: Path

    def workflow_path(self, workflow_id: str, version: int | None = None) -> Path:
        workflow_dir = self.workflow_root / workflow_id
        if version is None:
            return workflow_dir / "workflow.xml"
        return workflow_dir / "versions" / f"workflow_v{version}.xml"

    def step_plan_json_path(
        self,
        workflow_id: str,
        step_plan_id: str,
        version: int | None = None,
    ) -> Path:
        plan_dir = self.plan_root / workflow_id / step_plan_id
        if version is None:
            return plan_dir / "step_plan_latest.json"
        return plan_dir / f"step_plan_v{version}.json"


@dataclass(slots=True)
class AppRuntime:
    workflow_api: WorkflowAPI
    approval_api: ApprovalAPI
    run_api: RunAPI
    workflow_repo: XmlWorkflowRepository
    step_plan_repo: FilesystemStepPlanRepository
    planner: IPlanner
    mcp_client: IMcpClient
    llm_client: ILlmClient | None
    audit_logger: FilesystemAuditLogger
    artifacts: RuntimeArtifacts
    metadata: RuntimeMetadata
    using_mock: bool

    def close(self) -> None:
        close_mcp = getattr(self.mcp_client, "close", None)
        if callable(close_mcp):
            close_mcp()
        close_llm = getattr(self.llm_client, "close", None)
        if callable(close_llm):
            close_llm()
        close_audit = getattr(self.audit_logger, "close", None)
        if callable(close_audit):
            close_audit()
