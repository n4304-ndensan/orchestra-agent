from __future__ import annotations

import argparse
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from orchestra_agent.runtime import LlmProviderName, PlannerMode

CONFIG_ENV_VAR = "ORCHESTRA_CONFIG"
type McpTransport = Literal["http", "stdio"]
type McpToolGroup = Literal["all", "files", "excel"]


@dataclass(slots=True)
class WorkspaceSettings:
    root: str = "."
    workflow_root: str = "workflow"
    plan_root: str = "plan"
    snapshots_dir: str = ".orchestra_snapshots"
    state_root: str = ".orchestra_state/runs"
    audit_root: str = ".orchestra_state/audit"


@dataclass(slots=True)
class McpServerSettings:
    name: str = "orchestra-workspace"
    transport: McpTransport = "http"
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    endpoint: str | None = None
    tool_group: McpToolGroup = "all"


@dataclass(slots=True)
class McpSettings(McpServerSettings):
    servers: tuple[McpServerSettings, ...] = ()

    def runtime_endpoints(self) -> tuple[str, ...]:
        if self.servers:
            endpoints = tuple(
                server.endpoint.strip()
                for server in self.servers
                if server.endpoint is not None and server.endpoint.strip()
            )
            if endpoints:
                return endpoints
        if self.endpoint is None or not self.endpoint.strip():
            return ()
        return (self.endpoint.strip(),)

    def resolve_server(self, name: str | None) -> McpServerSettings:
        if name is None:
            return McpServerSettings(
                name=self.name,
                transport=self.transport,
                host=self.host,
                port=self.port,
                path=self.path,
                endpoint=self.endpoint,
                tool_group=self.tool_group,
            )

        for server in self.servers:
            if server.name == name:
                return server
        raise KeyError(f"MCP server profile '{name}' was not found in config.")


@dataclass(slots=True)
class ApiSettings:
    host: str = "127.0.0.1"
    port: int = 9000


@dataclass(slots=True)
class LlmSettings:
    provider: LlmProviderName = "none"
    proposal_file: str | None = None
    planner_mode: PlannerMode | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_api_key_env: str = "OPENAI_API_KEY"
    openai_base_url: str = "https://api.openai.com"
    openai_timeout: float = 60.0
    google_model: str = "gemini-2.5-flash"
    google_api_key_env: str = "GEMINI_API_KEY"
    google_base_url: str = "https://generativelanguage.googleapis.com"
    google_timeout: float = 60.0
    temperature: float = 0.0
    max_tokens: int = 1200


@dataclass(slots=True)
class RuntimeSettings:
    workflow_name: str = "Automation Workflow"
    run_id: str = "run-cli"
    auto_approve: bool = True
    max_resume: int = 50
    print_plan: bool = True
    repair_max_attempts: int = 3


@dataclass(slots=True)
class AppConfig:
    source_path: Path | None = None
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    mcp: McpSettings = field(default_factory=McpSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], source_path: Path | None = None) -> AppConfig:
        workspace_payload = _as_dict(payload.get("workspace", {}))
        mcp_payload = _as_dict(payload.get("mcp", {}))
        api_payload = _as_dict(payload.get("api", {}))
        llm_payload = _as_dict(payload.get("llm", {}))
        runtime_payload = _as_dict(payload.get("runtime", {}))

        return cls(
            source_path=source_path,
            workspace=WorkspaceSettings(
                root=_as_str(workspace_payload.get("root"), "."),
                workflow_root=_as_str(workspace_payload.get("workflow_root"), "workflow"),
                plan_root=_as_str(workspace_payload.get("plan_root"), "plan"),
                snapshots_dir=_as_str(
                    workspace_payload.get("snapshots_dir"),
                    ".orchestra_snapshots",
                ),
                state_root=_as_str(workspace_payload.get("state_root"), ".orchestra_state/runs"),
                audit_root=_as_str(
                    workspace_payload.get("audit_root"),
                    ".orchestra_state/audit",
                ),
            ),
            mcp=McpSettings(
                name=_as_str(mcp_payload.get("name"), "orchestra-workspace"),
                transport=_as_transport(mcp_payload.get("transport"), "http"),
                host=_as_str(mcp_payload.get("host"), "127.0.0.1"),
                port=_as_int(mcp_payload.get("port"), 8000),
                path=_as_str(mcp_payload.get("path"), "/mcp"),
                endpoint=_as_optional_str(mcp_payload.get("endpoint")),
                tool_group=_as_tool_group(mcp_payload.get("tool_group"), "all"),
                servers=tuple(_as_mcp_server_settings(mcp_payload.get("servers"))),
            ),
            api=ApiSettings(
                host=_as_str(api_payload.get("host"), "127.0.0.1"),
                port=_as_int(api_payload.get("port"), 9000),
            ),
            llm=LlmSettings(
                provider=_as_llm_provider(llm_payload.get("provider"), "none"),
                proposal_file=_as_optional_str(llm_payload.get("proposal_file")),
                planner_mode=_as_optional_planner_mode(llm_payload.get("planner_mode")),
                openai_model=_as_str(llm_payload.get("openai_model"), "gpt-4.1-mini"),
                openai_api_key_env=_as_str(
                    llm_payload.get("openai_api_key_env"),
                    "OPENAI_API_KEY",
                ),
                openai_base_url=_as_str(
                    llm_payload.get("openai_base_url"),
                    "https://api.openai.com",
                ),
                openai_timeout=_as_float(llm_payload.get("openai_timeout"), 60.0),
                google_model=_as_str(llm_payload.get("google_model"), "gemini-2.5-flash"),
                google_api_key_env=_as_str(
                    llm_payload.get("google_api_key_env"),
                    "GEMINI_API_KEY",
                ),
                google_base_url=_as_str(
                    llm_payload.get("google_base_url"),
                    "https://generativelanguage.googleapis.com",
                ),
                google_timeout=_as_float(llm_payload.get("google_timeout"), 60.0),
                temperature=_as_float(llm_payload.get("temperature"), 0.0),
                max_tokens=_as_int(llm_payload.get("max_tokens"), 1200),
            ),
            runtime=RuntimeSettings(
                workflow_name=_as_str(
                    runtime_payload.get("workflow_name"),
                    "Automation Workflow",
                ),
                run_id=_as_str(runtime_payload.get("run_id"), "run-cli"),
                auto_approve=_as_bool(runtime_payload.get("auto_approve"), True),
                max_resume=_as_int(runtime_payload.get("max_resume"), 50),
                print_plan=_as_bool(runtime_payload.get("print_plan"), True),
                repair_max_attempts=_as_int(
                    runtime_payload.get("repair_max_attempts"),
                    3,
                ),
            ),
        )

    def resolve_workspace(self, raw_path: str | None = None) -> Path:
        return _resolve_from_base(raw_path or self.workspace.root, self._config_dir())

    @staticmethod
    def resolve_within_workspace(raw_path: str, workspace: Path) -> Path:
        return _resolve_from_base(raw_path, workspace)

    def _config_dir(self) -> Path:
        if self.source_path is None:
            return Path.cwd()
        return self.source_path.parent


def load_app_config(path: Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Config file '{resolved}' was not found.")
    with resolved.open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Config file must decode to a TOML table.")
    return AppConfig.from_dict(payload, source_path=resolved)


def resolve_config_path(argv: list[str] | None = None) -> Path | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=None)
    parsed, _ = parser.parse_known_args(argv)
    raw_path = parsed.config or os.getenv(CONFIG_ENV_VAR)
    if raw_path is None or not str(raw_path).strip():
        return None
    return Path(str(raw_path))


def _resolve_from_base(raw_path: str, base_dir: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Config sections must be TOML tables.")
    return value


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("Config value must be a string.")
    return value


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Config value must be a string or null.")
    return value


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("Config value must be a boolean.")
    return value


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Config value must be an integer.")
    return value


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Config value must be numeric.")
    return float(value)


def _as_transport(value: Any, default: McpTransport) -> McpTransport:
    if value is None:
        return default
    if value not in ("http", "stdio"):
        raise ValueError("mcp.transport must be 'http' or 'stdio'.")
    return cast(McpTransport, value)


def _as_tool_group(value: Any, default: McpToolGroup) -> McpToolGroup:
    if value is None:
        return default
    if value not in ("all", "files", "excel"):
        raise ValueError("mcp.tool_group must be 'all', 'files', or 'excel'.")
    return cast(McpToolGroup, value)


def _as_mcp_server_settings(value: Any) -> list[McpServerSettings]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("mcp.servers must be an array of TOML tables.")

    servers: list[McpServerSettings] = []
    for item in value:
        payload = _as_dict(item)
        servers.append(
            McpServerSettings(
                name=_as_str(payload.get("name"), "orchestra-workspace"),
                transport=_as_transport(payload.get("transport"), "http"),
                host=_as_str(payload.get("host"), "127.0.0.1"),
                port=_as_int(payload.get("port"), 8000),
                path=_as_str(payload.get("path"), "/mcp"),
                endpoint=_as_optional_str(payload.get("endpoint")),
                tool_group=_as_tool_group(payload.get("tool_group"), "all"),
            )
        )
    return servers


def _as_llm_provider(value: Any, default: LlmProviderName) -> LlmProviderName:
    if value is None:
        return default
    if value not in ("none", "file", "openai", "google"):
        raise ValueError("llm.provider must be one of: none, file, openai, google.")
    return cast(LlmProviderName, value)


def _as_optional_planner_mode(value: Any) -> PlannerMode | None:
    if value is None:
        return None
    if value not in ("deterministic", "augmented", "full"):
        raise ValueError("llm.planner_mode must be deterministic, augmented, or full.")
    return cast(PlannerMode, value)
