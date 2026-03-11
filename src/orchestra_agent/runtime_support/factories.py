from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from orchestra_agent import __version__
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
from orchestra_agent.observability import LoggingLlmClient, LoggingMcpClient
from orchestra_agent.ports import ILlmClient, IMcpClient, IPlanner
from orchestra_agent.runtime_support.models import (
    AppRuntime,
    PlannerMode,
    RuntimeArtifacts,
    RuntimeConfig,
    RuntimeMetadata,
)
from orchestra_agent.runtime_support.pathing import (
    describe_mcp_tools,
    normalize_mcp_endpoints,
    resolve_file_arg,
)


@dataclass(slots=True)
class McpClientBundle:
    client: IMcpClient
    using_mock: bool


@dataclass(slots=True)
class LlmProviderBundle:
    proposal_provider: IStepProposalProvider | None
    llm_client: OpenAILlmClient | GoogleGeminiLlmClient | None


class IMcpClientFactory(Protocol):
    def create(self, config: RuntimeConfig) -> McpClientBundle:
        ...


class ILlmProviderFactory(Protocol):
    def create(self, config: RuntimeConfig) -> LlmProviderBundle:
        ...


class IPlannerFactory(Protocol):
    def create(
        self,
        config: RuntimeConfig,
        *,
        mcp_client: IMcpClient,
        proposal_provider: IStepProposalProvider | None,
        llm_client: ILlmClient | None,
    ) -> IPlanner:
        ...


class IRuntimeFactory(Protocol):
    def create(self, config: RuntimeConfig) -> AppRuntime:
        ...


class DefaultMcpClientFactory(IMcpClientFactory):
    def create(self, config: RuntimeConfig) -> McpClientBundle:
        endpoints = normalize_mcp_endpoints(config.mcp_endpoints, config.mcp_endpoint)
        if not endpoints:
            return McpClientBundle(client=MockExcelMcpClient(), using_mock=True)
        if len(endpoints) == 1:
            return McpClientBundle(client=JsonRpcMcpClient(endpoint=endpoints[0]), using_mock=False)
        return McpClientBundle(client=MultiEndpointMcpClient(endpoints=endpoints), using_mock=False)


class DefaultLlmProviderFactory(ILlmProviderFactory):
    def __init__(
        self,
        *,
        openai_client_type: type[OpenAILlmClient] = OpenAILlmClient,
        google_client_type: type[GoogleGeminiLlmClient] = GoogleGeminiLlmClient,
    ) -> None:
        self._openai_client_type = openai_client_type
        self._google_client_type = google_client_type

    def create(self, config: RuntimeConfig) -> LlmProviderBundle:
        if config.llm_provider == "none":
            return LlmProviderBundle(proposal_provider=None, llm_client=None)

        if config.llm_provider == "file":
            if config.llm_proposal_file is None:
                raise ValueError("--llm-proposal-file is required when --llm-provider file.")
            proposal_path = resolve_file_arg(config.llm_proposal_file, config.workspace)
            return LlmProviderBundle(
                proposal_provider=JsonFileStepProposalProvider(proposal_path),
                llm_client=None,
            )

        verify = self._resolve_tls_verify(config)

        if config.llm_provider == "openai":
            api_key = self._required_env(config.llm_openai_api_key_env, "OpenAI LLM")
            llm_client = self._openai_client_type(
                api_key=api_key,
                model=config.llm_openai_model,
                base_url=config.llm_openai_base_url,
                timeout_seconds=config.llm_openai_timeout,
                verify=verify,
            )
            return LlmProviderBundle(
                proposal_provider=LlmStepProposalProvider(
                    llm_client=llm_client,
                    language=config.llm_language,
                    temperature=config.llm_temperature,
                    max_tokens=config.llm_max_tokens,
                ),
                llm_client=llm_client,
            )

        api_key = os.getenv(config.llm_google_api_key_env) or os.getenv("GOOGLE_API_KEY")
        if api_key is None or not api_key.strip():
            raise ValueError(
                "Google Gemini API key is required. Set "
                f"'{config.llm_google_api_key_env}' or 'GOOGLE_API_KEY'."
            )
        google_client = self._google_client_type(
            api_key=api_key,
            model=config.llm_google_model,
            base_url=config.llm_google_base_url,
            timeout_seconds=config.llm_google_timeout,
            verify=verify,
        )
        return LlmProviderBundle(
            proposal_provider=LlmStepProposalProvider(
                llm_client=google_client,
                language=config.llm_language,
                temperature=config.llm_temperature,
                max_tokens=config.llm_max_tokens,
            ),
            llm_client=google_client,
        )

    @staticmethod
    def _required_env(env_name: str, label: str) -> str:
        value = os.getenv(env_name)
        if value is None or not value.strip():
            raise ValueError(f"Environment variable '{env_name}' is required for {label}.")
        return value

    @staticmethod
    def _resolve_tls_verify(config: RuntimeConfig) -> bool | str:
        verify: bool | str = config.llm_tls_verify
        if config.llm_tls_ca_bundle is not None:
            if not config.llm_tls_ca_bundle.is_file():
                raise ValueError(
                    "LLM TLS CA bundle was not found: "
                    f"'{config.llm_tls_ca_bundle}'. "
                    "Set llm.tls_ca_bundle to an existing certificate file path."
                )
            verify = str(config.llm_tls_ca_bundle)
        return verify


class DefaultPlannerFactory(IPlannerFactory):
    def create(
        self,
        config: RuntimeConfig,
        *,
        mcp_client: IMcpClient,
        proposal_provider: IStepProposalProvider | None,
        llm_client: ILlmClient | None,
    ) -> IPlanner:
        planner_mode = resolve_planner_mode(config)
        base_planner = LlmPlanner(plan_style="abstract" if planner_mode == "full" else "concrete")
        if planner_mode == "full" and llm_client is not None:
            return StructuredLlmPlanner(
                llm_client=llm_client,
                available_tools_supplier=mcp_client.list_tools,
                available_tool_catalog_supplier=lambda: describe_mcp_tools(mcp_client),
                fallback_planner=base_planner,
                language=config.llm_language,
                temperature=config.llm_temperature,
                max_tokens=config.llm_max_tokens,
            )

        active_proposal_provider = proposal_provider if planner_mode == "augmented" else None
        return SafeAugmentedLlmPlanner(
            base_planner=base_planner,
            proposal_provider=active_proposal_provider,
        )


class DefaultRuntimeFactory(IRuntimeFactory):
    def __init__(
        self,
        *,
        mcp_client_factory: IMcpClientFactory | None = None,
        llm_provider_factory: ILlmProviderFactory | None = None,
        planner_factory: IPlannerFactory | None = None,
    ) -> None:
        self._mcp_client_factory = mcp_client_factory or DefaultMcpClientFactory()
        self._llm_provider_factory = llm_provider_factory or DefaultLlmProviderFactory()
        self._planner_factory = planner_factory or DefaultPlannerFactory()

    def create(self, config: RuntimeConfig) -> AppRuntime:
        state_store = FilesystemAgentStateStore(config.state_root)
        audit_logger = FilesystemAuditLogger(config.audit_root)
        workflow_repo = XmlWorkflowRepository(config.workflow_root, audit_logger=audit_logger)
        step_plan_repo = FilesystemStepPlanRepository(config.plan_root, audit_logger=audit_logger)
        normalized_endpoints = normalize_mcp_endpoints(config.mcp_endpoints, config.mcp_endpoint)

        mcp_bundle = self._mcp_client_factory.create(config)
        mcp_client = LoggingMcpClient(mcp_bundle.client, audit_logger)

        llm_bundle = self._llm_provider_factory.create(config)
        llm_client = self._build_logged_llm_client(llm_bundle.llm_client, audit_logger)
        proposal_provider = self._build_proposal_provider(config, llm_bundle, llm_client)
        planner = self._planner_factory.create(
            config,
            mcp_client=mcp_client,
            proposal_provider=proposal_provider,
            llm_client=llm_client,
        )

        snapshot_manager = FilesystemSnapshotManager(
            config.snapshots_dir,
            workspace_root=config.workspace,
        )
        policy_engine = DefaultPolicyEngine()
        step_executor = self._build_step_executor(config, llm_client, audit_logger)
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

        workflow_api = WorkflowAPI(
            CreateWorkflowUseCase(workflow_repo, audit_logger),
            CompileStepPlanUseCase(planner, policy_engine, step_plan_repo, audit_logger),
            workflow_repo,
        )
        approval_api = ApprovalAPI(
            ApproveStepPlanUseCase(step_plan_repo, audit_logger),
            step_plan_repo,
        )
        run_api = RunAPI(
            ExecutePlanUseCase(executor, state_store, audit_logger),
            workflow_repo,
            step_plan_repo,
            state_store,
        )
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
            artifacts=RuntimeArtifacts(
                workspace_root=config.workspace,
                workflow_root=config.workflow_root,
                plan_root=config.plan_root,
                snapshots_dir=config.snapshots_dir,
                state_root=config.state_root,
                audit_root=config.audit_root,
            ),
            metadata=RuntimeMetadata(
                app_version=__version__,
                llm_provider=config.llm_provider,
                planner_mode=resolve_planner_mode(config),
                llm_language=config.llm_language,
                llm_remembers_context=config.llm_remembers_context,
                mcp_endpoints=normalized_endpoints,
            ),
            using_mock=mcp_bundle.using_mock,
        )

    @staticmethod
    def _build_logged_llm_client(
        llm_client: OpenAILlmClient | GoogleGeminiLlmClient | None,
        audit_logger: FilesystemAuditLogger,
    ) -> ILlmClient | None:
        if llm_client is None:
            return None
        return LoggingLlmClient(llm_client, audit_logger)

    @staticmethod
    def _build_proposal_provider(
        config: RuntimeConfig,
        llm_bundle: LlmProviderBundle,
        llm_client: ILlmClient | None,
    ) -> IStepProposalProvider | None:
        if (
            isinstance(llm_bundle.proposal_provider, LlmStepProposalProvider)
            and llm_client is not None
        ):
            return LlmStepProposalProvider(
                llm_client=llm_client,
                language=config.llm_language,
                temperature=config.llm_temperature,
                max_tokens=config.llm_max_tokens,
            )
        return llm_bundle.proposal_provider

    @staticmethod
    def _build_step_executor(
        config: RuntimeConfig,
        llm_client: ILlmClient | None,
        audit_logger: FilesystemAuditLogger,
    ) -> LlmStepExecutor | None:
        if llm_client is None:
            return None
        return LlmStepExecutor(
            llm_client=llm_client,
            workspace_root=config.workspace,
            language=config.llm_language,
            remembers_context=config.llm_remembers_context,
            temperature=config.llm_temperature,
            max_tokens=config.llm_max_tokens,
            audit_logger=audit_logger,
        )


def build_llm_provider(
    config: RuntimeConfig,
    *,
    factory: ILlmProviderFactory | None = None,
) -> tuple[IStepProposalProvider | None, OpenAILlmClient | GoogleGeminiLlmClient | None]:
    llm_bundle = (factory or DefaultLlmProviderFactory()).create(config)
    return llm_bundle.proposal_provider, llm_bundle.llm_client


def resolve_planner_mode(config: RuntimeConfig) -> PlannerMode:
    if config.llm_planner_mode is not None:
        return config.llm_planner_mode
    if config.llm_provider in ("openai", "google"):
        return "full"
    if config.llm_provider == "file":
        return "augmented"
    return "deterministic"
