from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestra_agent.adapters import (
    DefaultPolicyEngine,
    FilesystemAgentStateStore,
    FilesystemAuditLogger,
    FilesystemSnapshotManager,
    FilesystemStepPlanRepository,
    GoogleGeminiLlmClient,
    JsonFileStepProposalProvider,
    JsonRpcMcpClient,
    LlmPlanner,
    LlmStepExecutor,
    LlmStepProposalProvider,
    MockExcelMcpClient,
    MultiEndpointMcpClient,
    OpenAILlmClient,
    SafeAugmentedLlmPlanner,
    StructuredLlmPlanner,
    XmlWorkflowRepository,
)
from orchestra_agent.adapters.planner import IStepProposalProvider
from orchestra_agent.api import ApprovalAPI, RunAPI, WorkflowAPI
from orchestra_agent.application.use_cases import (
    ApproveStepPlanUseCase,
    CompileStepPlanUseCase,
    CreateWorkflowUseCase,
    ExecutePlanUseCase,
)
from orchestra_agent.executor import FailureHandler, PlanExecutor
from orchestra_agent.ports import ILlmClient, IMcpClient, IPlanner

type LlmProviderName = Literal["none", "file", "openai", "google"]
type PlannerMode = Literal["deterministic", "augmented", "full"]


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
    llm_proposal_file: str | None = None
    llm_openai_model: str = "gpt-4.1-mini"
    llm_openai_api_key_env: str = "OPENAI_API_KEY"
    llm_openai_base_url: str = "https://api.openai.com"
    llm_openai_timeout: float = 60.0
    llm_google_model: str = "gemini-2.5-flash"
    llm_google_api_key_env: str = "GEMINI_API_KEY"
    llm_google_base_url: str = "https://generativelanguage.googleapis.com"
    llm_google_timeout: float = 60.0
    llm_planner_mode: PlannerMode | None = None
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1200
    repair_max_attempts: int = 3


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
    using_mock: bool

    def close(self) -> None:
        close_mcp = getattr(self.mcp_client, "close", None)
        if callable(close_mcp):
            close_mcp()
        close_llm = getattr(self.llm_client, "close", None)
        if callable(close_llm):
            close_llm()


def resolve_path(value: str, workspace: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((workspace / path).resolve())


def resolve_file_arg(value: str, workspace: Path) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        return raw
    if raw.exists():
        return raw.resolve()
    return (workspace / raw).resolve()


def build_runtime(config: RuntimeConfig) -> AppRuntime:
    workflow_repo = XmlWorkflowRepository(config.workflow_root)
    step_plan_repo = FilesystemStepPlanRepository(config.plan_root)
    state_store = FilesystemAgentStateStore(config.state_root)
    audit_logger = FilesystemAuditLogger(config.audit_root)

    endpoints = _normalize_mcp_endpoints(config.mcp_endpoints, config.mcp_endpoint)
    using_mock = not endpoints
    if using_mock:
        mcp_client: IMcpClient = MockExcelMcpClient()
    elif len(endpoints) == 1:
        mcp_client = JsonRpcMcpClient(endpoint=endpoints[0])
    else:
        mcp_client = MultiEndpointMcpClient(endpoints=endpoints)

    base_planner = LlmPlanner()
    proposal_provider, llm_client = _build_llm_provider(config)
    planner_mode = _resolve_planner_mode(config)
    if planner_mode == "full" and llm_client is not None:
        planner: IPlanner = StructuredLlmPlanner(
            llm_client=llm_client,
            available_tools_supplier=mcp_client.list_tools,
            available_tool_catalog_supplier=lambda: _describe_mcp_tools(mcp_client),
            fallback_planner=base_planner,
            temperature=config.llm_temperature,
            max_tokens=config.llm_max_tokens,
        )
    else:
        active_proposal_provider = proposal_provider if planner_mode == "augmented" else None
        planner = SafeAugmentedLlmPlanner(
            base_planner=base_planner,
            proposal_provider=active_proposal_provider,
        )

    policy_engine = DefaultPolicyEngine()
    snapshot_manager = FilesystemSnapshotManager(
        config.snapshots_dir,
        workspace_root=config.workspace,
    )
    step_executor = None
    if llm_client is not None:
        step_executor = LlmStepExecutor(
            llm_client=llm_client,
            workspace_root=config.workspace,
            temperature=config.llm_temperature,
            max_tokens=config.llm_max_tokens,
        )

    compile_uc = CompileStepPlanUseCase(planner, policy_engine, step_plan_repo, audit_logger)
    create_workflow_uc = CreateWorkflowUseCase(workflow_repo, audit_logger)
    approve_uc = ApproveStepPlanUseCase(step_plan_repo, audit_logger)
    failure_handler = FailureHandler(
        snapshot_manager=snapshot_manager,
        planner=planner,
        policy_engine=policy_engine,
        step_plan_repository=step_plan_repo,
        audit_logger=audit_logger,
        workflow_repository=workflow_repo,
        max_replans=config.repair_max_attempts,
    )
    executor = PlanExecutor(
        mcp_client=mcp_client,
        state_store=state_store,
        snapshot_manager=snapshot_manager,
        audit_logger=audit_logger,
        failure_handler=failure_handler,
        step_executor=step_executor,
    )
    execute_uc = ExecutePlanUseCase(executor, state_store, audit_logger)

    workflow_api = WorkflowAPI(create_workflow_uc, compile_uc, workflow_repo)
    approval_api = ApprovalAPI(approve_uc, step_plan_repo)
    run_api = RunAPI(execute_uc, workflow_repo, step_plan_repo, state_store)

    return AppRuntime(
        workflow_api=workflow_api,
        approval_api=approval_api,
        run_api=run_api,
        workflow_repo=workflow_repo,
        step_plan_repo=step_plan_repo,
        planner=planner,
        mcp_client=mcp_client,
        llm_client=llm_client,
        audit_logger=audit_logger,
        using_mock=using_mock,
    )


def _build_llm_provider(
    config: RuntimeConfig,
) -> tuple[IStepProposalProvider | None, OpenAILlmClient | GoogleGeminiLlmClient | None]:
    if config.llm_provider == "none":
        return None, None

    if config.llm_provider == "file":
        if config.llm_proposal_file is None:
            raise ValueError("--llm-proposal-file is required when --llm-provider file.")
        proposal_path = resolve_file_arg(config.llm_proposal_file, config.workspace)
        return JsonFileStepProposalProvider(proposal_path), None

    if config.llm_provider == "openai":
        api_key = os.getenv(config.llm_openai_api_key_env)
        if api_key is None or not api_key.strip():
            raise ValueError(
                "Environment variable "
                f"'{config.llm_openai_api_key_env}' is required for OpenAI LLM."
            )
        llm_client = OpenAILlmClient(
            api_key=api_key,
            model=config.llm_openai_model,
            base_url=config.llm_openai_base_url,
            timeout_seconds=config.llm_openai_timeout,
        )
        provider = LlmStepProposalProvider(
            llm_client=llm_client,
            temperature=config.llm_temperature,
            max_tokens=config.llm_max_tokens,
        )
        return provider, llm_client

    api_key = os.getenv(config.llm_google_api_key_env) or os.getenv("GOOGLE_API_KEY")
    if api_key is None or not api_key.strip():
        raise ValueError(
            "Google Gemini API key is required. Set "
            f"'{config.llm_google_api_key_env}' or 'GOOGLE_API_KEY'."
        )
    google_client = GoogleGeminiLlmClient(
        api_key=api_key,
        model=config.llm_google_model,
        base_url=config.llm_google_base_url,
        timeout_seconds=config.llm_google_timeout,
    )
    provider = LlmStepProposalProvider(
        llm_client=google_client,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
    )
    return provider, google_client


def _resolve_planner_mode(config: RuntimeConfig) -> PlannerMode:
    if config.llm_planner_mode is not None:
        return config.llm_planner_mode
    if config.llm_provider in ("openai", "google"):
        return "full"
    if config.llm_provider == "file":
        return "augmented"
    return "deterministic"


def _normalize_mcp_endpoints(
    configured_endpoints: tuple[str, ...],
    legacy_endpoint: str | None,
) -> tuple[str, ...]:
    normalized: list[str] = []

    for endpoint in configured_endpoints:
        if endpoint.strip() and endpoint not in normalized:
            normalized.append(endpoint)

    if (
        legacy_endpoint is not None
        and legacy_endpoint.strip()
        and legacy_endpoint not in normalized
    ):
        normalized.append(legacy_endpoint)

    return tuple(normalized)


def _describe_mcp_tools(mcp_client: IMcpClient) -> list[dict[str, str]]:
    describe_tools = getattr(mcp_client, "describe_tools", None)
    if callable(describe_tools):
        raw_tools = describe_tools()
        described_tools: list[dict[str, str]] = []
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, dict):
                continue
            name = raw_tool.get("name")
            if not isinstance(name, str):
                continue
            description = raw_tool.get("description")
            described_tools.append(
                {
                    "name": name,
                    "description": description if isinstance(description, str) else "",
                }
            )
        if described_tools:
            return described_tools

    return [{"name": tool_name, "description": ""} for tool_name in mcp_client.list_tools()]
