from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestra_agent.runtime import (
    RuntimeConfig,
    build_runtime,
    describe_mcp_tools,
    resolve_mcp_endpoints,
)
from orchestra_agent.runtime_support.factories import resolve_planner_mode


class _RecordingRuntimeFactory:
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self.seen: RuntimeConfig | None = None

    def create(self, config: RuntimeConfig) -> object:
        self.seen = config
        return self.runtime


class _DescribeToolsClient:
    def describe_tools(self) -> list[dict[str, str]]:
        return [{"name": "excel.open_file", "description": "Open workbook", "server": "excel"}]

    def list_tools(self) -> list[str]:
        return ["ignored"]


class _ListToolsOnlyClient:
    def list_tools(self) -> list[str]:
        return ["fs_write_text", "excel.read_sheet"]


def test_build_runtime_delegates_to_injected_factory() -> None:
    expected_runtime = object()
    factory = _RecordingRuntimeFactory(expected_runtime)
    config = RuntimeConfig(
        workspace=Path("."),
        snapshots_dir=Path(".orchestra_snapshots"),
        workflow_root=Path("workflow"),
        plan_root=Path("plan"),
        state_root=Path(".orchestra_state/runs"),
        audit_root=Path(".orchestra_state/audit"),
    )

    runtime = build_runtime(config, factory=factory)

    assert runtime is expected_runtime
    assert factory.seen == config


def test_resolve_mcp_endpoints_prefers_cli_values_and_normalizes() -> None:
    config = SimpleNamespace(
        mcp=SimpleNamespace(
            runtime_endpoints=lambda: ("http://config-a/mcp", "http://config-b/mcp")
        )
    )

    resolved = resolve_mcp_endpoints(
        [" http://cli-a/mcp ", "", "http://cli-a/mcp", "http://cli-b/mcp"],
        config,
    )

    assert resolved == ("http://cli-a/mcp", "http://cli-b/mcp")


def test_describe_mcp_tools_uses_descriptions_when_available() -> None:
    tools = describe_mcp_tools(_DescribeToolsClient())

    assert tools == [
        {
            "name": "excel.open_file",
            "description": "Open workbook",
            "server": "excel",
        }
    ]


def test_describe_mcp_tools_falls_back_to_list_tools() -> None:
    tools = describe_mcp_tools(_ListToolsOnlyClient())

    assert tools == [
        {"name": "fs_write_text", "description": ""},
        {"name": "excel.read_sheet", "description": ""},
    ]


def test_resolve_planner_mode_defaults_custom_provider_to_full() -> None:
    config = RuntimeConfig(
        workspace=Path("."),
        snapshots_dir=Path(".orchestra_snapshots"),
        workflow_root=Path("workflow"),
        plan_root=Path("plan"),
        state_root=Path(".orchestra_state/runs"),
        audit_root=Path(".orchestra_state/audit"),
        llm_provider="custom_provider",
    )

    assert resolve_planner_mode(config) == "full"
