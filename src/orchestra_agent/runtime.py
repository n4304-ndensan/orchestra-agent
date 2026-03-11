from orchestra_agent.adapters import (
    ChatGptPlaywrightLlmClient,
    GoogleGeminiLlmClient,
    OpenAILlmClient,
)
from orchestra_agent.runtime_support import (
    AppRuntime,
    DefaultLlmProviderFactory,
    DefaultRuntimeFactory,
    IRuntimeFactory,
    LlmLanguage,
    LlmProviderName,
    PlannerMode,
    RuntimeArtifacts,
    RuntimeConfig,
    RuntimeMetadata,
    describe_mcp_tools,
    resolve_file_arg,
    resolve_mcp_endpoints,
    resolve_path,
)
from orchestra_agent.runtime_support.factories import build_llm_provider


def build_runtime(
    config: RuntimeConfig,
    *,
    factory: IRuntimeFactory | None = None,
) -> AppRuntime:
    return (factory or DefaultRuntimeFactory()).create(config)


def _build_llm_provider(config: RuntimeConfig):
    return build_llm_provider(
        config,
        factory=DefaultLlmProviderFactory(
            chatgpt_playwright_client_type=ChatGptPlaywrightLlmClient,
            openai_client_type=OpenAILlmClient,
            google_client_type=GoogleGeminiLlmClient,
        ),
    )


__all__ = [
    "AppRuntime",
    "IRuntimeFactory",
    "LlmLanguage",
    "LlmProviderName",
    "PlannerMode",
    "RuntimeArtifacts",
    "RuntimeConfig",
    "RuntimeMetadata",
    "build_runtime",
    "describe_mcp_tools",
    "resolve_file_arg",
    "resolve_mcp_endpoints",
    "resolve_path",
    "_build_llm_provider",
]
