from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol

from orchestra_agent.adapters.planner import IStepProposalProvider
from orchestra_agent.ports import ILlmClient
from orchestra_agent.runtime_support.models import RuntimeConfig


@dataclass(slots=True)
class LlmProviderBundle:
    proposal_provider: IStepProposalProvider | None
    llm_client: ILlmClient | None


type LlmProviderBuilder = Callable[[RuntimeConfig], LlmProviderBundle]


@dataclass(slots=True, frozen=True)
class LlmProviderDefinition:
    name: str
    builder: LlmProviderBuilder
    source: str


class ILlmProviderModule(Protocol):
    PROVIDER_NAME: str

    def build_llm_provider(config: RuntimeConfig) -> LlmProviderBundle:
        ...


def load_llm_provider_definitions(
    module_names: tuple[str, ...],
) -> dict[str, LlmProviderDefinition]:
    definitions: dict[str, LlmProviderDefinition] = {}
    for module_name in module_names:
        module = import_module(module_name)
        provider_name = getattr(module, "PROVIDER_NAME", None)
        builder = getattr(module, "build_llm_provider", None)
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise ValueError(
                f"LLM provider module '{module_name}' must define non-empty PROVIDER_NAME."
            )
        if not callable(builder):
            raise ValueError(
                f"LLM provider module '{module_name}' must define callable build_llm_provider."
            )
        normalized_name = provider_name.strip()
        if normalized_name in definitions:
            previous = definitions[normalized_name]
            raise ValueError(
                "Duplicate external LLM provider registration for "
                f"'{normalized_name}': '{previous.source}' and '{module_name}'."
            )
        definitions[normalized_name] = LlmProviderDefinition(
            name=normalized_name,
            builder=builder,
            source=module_name,
        )
    return definitions
