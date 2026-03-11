from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestra_agent.adapters.llm import GoogleGeminiLlmClient
from orchestra_agent.adapters.planner import LlmStepProposalProvider
from orchestra_agent.runtime import RuntimeConfig, _build_llm_provider
from orchestra_agent.runtime_support import DefaultLlmProviderFactory
from orchestra_agent.runtime_support.llm_provider_plugins import (
    LlmProviderBundle,
    LlmProviderDefinition,
)


def test_build_llm_provider_supports_google_api_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")

    provider, client = _build_llm_provider(
        RuntimeConfig(
            workspace=Path("."),
            workflow_root=Path("workflow"),
            plan_root=Path("plan"),
            snapshots_dir=Path(".orchestra_snapshots"),
            state_root=Path(".orchestra_state/runs"),
            audit_root=Path(".orchestra_state/audit"),
            llm_provider="google",
        )
    )

    assert isinstance(provider, LlmStepProposalProvider)
    assert isinstance(client, GoogleGeminiLlmClient)
    client.close()


def test_build_llm_provider_requires_google_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="Google Gemini API key is required"):
        _build_llm_provider(
            RuntimeConfig(
                workspace=Path("."),
                workflow_root=Path("workflow"),
                plan_root=Path("plan"),
                snapshots_dir=Path(".orchestra_snapshots"),
                state_root=Path(".orchestra_state/runs"),
                audit_root=Path(".orchestra_state/audit"),
                llm_provider="google",
            )
        )


def test_build_llm_provider_passes_tls_ca_bundle_to_openai(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    ca_bundle = tmp_path / "company.crt"
    ca_bundle.write_text("dummy-cert", encoding="utf-8")

    captured: dict[str, Any] = {}

    class DummyOpenAILlmClient:
        def __init__(
            self,
            api_key: str,
            model: str,
            base_url: str,
            timeout_seconds: float,
            verify: bool | str,
        ) -> None:
            captured["api_key"] = api_key
            captured["model"] = model
            captured["base_url"] = base_url
            captured["timeout_seconds"] = timeout_seconds
            captured["verify"] = verify

        def close(self) -> None:
            return None

    provider, client = _build_llm_provider(
        RuntimeConfig(
            workspace=Path("."),
            workflow_root=Path("workflow"),
            plan_root=Path("plan"),
            snapshots_dir=Path(".orchestra_snapshots"),
            state_root=Path(".orchestra_state/runs"),
            audit_root=Path(".orchestra_state/audit"),
            llm_provider="openai",
            llm_tls_verify=True,
            llm_tls_ca_bundle=ca_bundle,
        ),
        factory=DefaultLlmProviderFactory(openai_client_type=DummyOpenAILlmClient),
    )

    assert isinstance(provider, LlmStepProposalProvider)
    assert isinstance(client, DummyOpenAILlmClient)
    assert Path(str(captured["verify"])) == ca_bundle


def test_build_llm_provider_rejects_missing_tls_ca_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    with pytest.raises(ValueError, match="LLM TLS CA bundle was not found"):
        _build_llm_provider(
            RuntimeConfig(
                workspace=Path("."),
                workflow_root=Path("workflow"),
                plan_root=Path("plan"),
                snapshots_dir=Path(".orchestra_snapshots"),
                state_root=Path(".orchestra_state/runs"),
                audit_root=Path(".orchestra_state/audit"),
                llm_provider="openai",
                llm_tls_ca_bundle=Path("missing-ca.crt"),
            )
        )

def test_build_llm_provider_supports_external_chatgpt_playwright_provider() -> None:
    captured: dict[str, Any] = {}

    class DummyChatGptPlaywrightLlmClient:
        def __init__(
            self,
            *,
            start_url: str,
            chrome_path: str,
            profile_dir: Path | None,
            port: int,
        ) -> None:
            captured["start_url"] = start_url
            captured["chrome_path"] = chrome_path
            captured["profile_dir"] = profile_dir
            captured["port"] = port

        def generate(self, request: Any) -> str:
            return "{}"

        def close(self) -> None:
            return None

    def build_chatgpt_provider(config: RuntimeConfig) -> LlmProviderBundle:
        llm_client = DummyChatGptPlaywrightLlmClient(
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

    provider, client = _build_llm_provider(
        RuntimeConfig(
            workspace=Path("."),
            workflow_root=Path("workflow"),
            plan_root=Path("plan"),
            snapshots_dir=Path(".orchestra_snapshots"),
            state_root=Path(".orchestra_state/runs"),
            audit_root=Path(".orchestra_state/audit"),
            llm_provider="chatgpt_playwright",
            llm_chatgpt_url="https://chatgpt.com/g/private-agent",
            llm_chatgpt_chrome_path=r"C:\Chrome\chrome.exe",
            llm_chatgpt_profile_dir=Path(".chatgpt-profile"),
            llm_chatgpt_port=9333,
        ),
        factory=DefaultLlmProviderFactory(
            external_provider_definitions={
                "chatgpt_playwright": LlmProviderDefinition(
                    name="chatgpt_playwright",
                    builder=build_chatgpt_provider,
                    source="tests.private.chatgpt_provider",
                )
            }
        ),
    )

    assert isinstance(provider, LlmStepProposalProvider)
    assert isinstance(client, DummyChatGptPlaywrightLlmClient)
    assert captured == {
        "start_url": "https://chatgpt.com/g/private-agent",
        "chrome_path": r"C:\Chrome\chrome.exe",
        "profile_dir": Path(".chatgpt-profile"),
        "port": 9333,
    }


def test_build_llm_provider_rejects_unknown_custom_provider() -> None:
    with pytest.raises(ValueError, match="llm.provider_modules"):
        _build_llm_provider(
            RuntimeConfig(
                workspace=Path("."),
                workflow_root=Path("workflow"),
                plan_root=Path("plan"),
                snapshots_dir=Path(".orchestra_snapshots"),
                state_root=Path(".orchestra_state/runs"),
                audit_root=Path(".orchestra_state/audit"),
                llm_provider="private_chatgpt",
            )
        )
