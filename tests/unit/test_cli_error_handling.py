from __future__ import annotations

import json

import pytest

from orchestra_agent.cli import main as run_cli


def test_cli_returns_structured_json_for_configuration_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    exit_code = run_cli(
        [
            "plan",
            "sales.xlsxのC列を集計してsummary.xlsxへ",
            "--llm-provider",
            "google",
            "--llm-planner-mode",
            "full",
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "error"
    assert payload["error"]["code"] == "invalid_configuration"
    assert "--llm-provider none" in payload["error"]["hint"]
