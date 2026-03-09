from __future__ import annotations

from pathlib import Path

import pytest

from orchestra_agent.adapters.llm import GoogleGeminiLlmClient
from orchestra_agent.adapters.planner import LlmStepProposalProvider
from orchestra_agent.runtime import RuntimeConfig, _build_llm_provider


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
