from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "ChatGptPlaywrightLlmClient",
    "GoogleGeminiLlmClient",
    "OpenAILlmClient",
]


def __getattr__(name: str) -> Any:
    if name == "ChatGptPlaywrightLlmClient":
        module = import_module("orchestra_agent.adapters.llm.chatgpt_playwright_llm_client")
        return module.ChatGptPlaywrightLlmClient
    if name == "GoogleGeminiLlmClient":
        module = import_module("orchestra_agent.adapters.llm.google_gemini_llm_client")
        return module.GoogleGeminiLlmClient
    if name == "OpenAILlmClient":
        module = import_module("orchestra_agent.adapters.llm.openai_llm_client")
        return module.OpenAILlmClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
