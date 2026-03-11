from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.config import AppConfig, load_app_config, resolve_config_path


@pytest.fixture()
def sandbox_dir() -> Iterator[Path]:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_app_config_reads_toml_and_resolves_workspace(sandbox_dir: Path) -> None:
    config_path = sandbox_dir / "orchestra-agent.toml"
    config_path.write_text(
        "\n".join(
            [
                "[workspace]",
                'root = "./runtime"',
                'workflow_root = "workflow"',
                "",
                "[mcp]",
                'endpoint = "http://orchestra-mcp:8000/mcp"',
                "",
                "[[mcp.servers]]",
                'name = "files"',
                'endpoint = "http://orchestra-mcp-files:8010/mcp"',
                'tool_group = "files"',
                "",
                "[[mcp.servers]]",
                'name = "excel"',
                'endpoint = "http://orchestra-mcp-excel:8020/mcp"',
                'tool_group = "excel"',
                "",
                "[llm]",
                'provider = "custom_provider"',
                'provider_modules = ["private_repo.orchestra.custom_provider"]',
                'language = "ja"',
                "remembers_context = true",
                "tls_verify = false",
                'tls_ca_bundle = "./certs/company.crt"',
                "",
                "[runtime]",
                "auto_approve = false",
                "interactive_approval = false",
            ]
        ),
        encoding="utf-8",
    )

    config = load_app_config(config_path)

    assert config.mcp.endpoint == "http://orchestra-mcp:8000/mcp"
    assert config.mcp.runtime_endpoints() == (
        "http://orchestra-mcp-files:8010/mcp",
        "http://orchestra-mcp-excel:8020/mcp",
    )
    assert config.mcp.resolve_server("excel").tool_group == "excel"
    assert config.llm.provider == "custom_provider"
    assert config.llm.provider_modules == ("private_repo.orchestra.custom_provider",)
    assert config.llm.language == "ja"
    assert config.llm.remembers_context is True
    assert config.llm.tls_verify is False
    assert config.llm.tls_ca_bundle == "./certs/company.crt"
    assert config.runtime.auto_approve is False
    assert config.runtime.interactive_approval is False
    assert config.resolve_workspace() == (sandbox_dir / "runtime").resolve()
    assert config.resolve_from_config(config.llm.tls_ca_bundle or "") == (
        sandbox_dir / "certs" / "company.crt"
    ).resolve()
    assert config.resolve_within_workspace("workflow", config.resolve_workspace()) == (
        sandbox_dir / "runtime" / "workflow"
    ).resolve()


def test_resolve_config_path_prefers_cli_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRA_CONFIG", "from-env.toml")

    resolved = resolve_config_path(["--config", "from-argv.toml"])

    assert resolved == Path("from-argv.toml")


def test_load_app_config_without_path_returns_defaults() -> None:
    config = load_app_config(None)

    assert isinstance(config, AppConfig)
    assert config.workspace.workflow_root == "workflow"
    assert config.runtime.run_id is None
