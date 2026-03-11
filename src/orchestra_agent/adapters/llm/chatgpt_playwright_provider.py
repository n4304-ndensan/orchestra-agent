from __future__ import annotations

from orchestra_agent.adapters.llm.chatgpt_playwright_llm_client import ChatGptPlaywrightLlmClient
from orchestra_agent.adapters.planner import LlmStepProposalProvider
from orchestra_agent.runtime_support.llm_provider_plugins import LlmProviderBundle
from orchestra_agent.runtime_support.models import RuntimeConfig

PROVIDER_NAME = "chatgpt_playwright"


def build_llm_provider(config: RuntimeConfig) -> LlmProviderBundle:
    llm_client = ChatGptPlaywrightLlmClient(
        start_url=config.llm_chatgpt_url,
        chrome_path=config.llm_chatgpt_chrome_path,
        profile_dir=config.llm_chatgpt_profile_dir,
        port=config.llm_chatgpt_port,
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
