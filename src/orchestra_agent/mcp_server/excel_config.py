from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

type SourceType = Literal[
    "local_workspace",
    "sharepoint_graph_workbook",
    "sharepoint_graph_roundtrip",
    "onedrive_graph_workbook",
    "onedrive_graph_roundtrip",
]


@dataclass(slots=True)
class ExcelPolicySettings:
    destructive_requires_preview: bool = True
    commit_requires_validation: bool = True
    allow_delete_sheet: bool = False
    allow_formula_write: bool = True
    allow_remote_overwrite: bool = False
    default_remote_mode: str = "sharepoint_graph_roundtrip"
    selected_scope_required: bool = False
    backup_retention_days: int = 30


@dataclass(slots=True)
class ExcelLimitSettings:
    inspect_workbook_timeout_sec: int = 15
    read_range_timeout_sec: int = 20
    preview_edit_timeout_sec: int = 30
    commit_local_timeout_sec: int = 60
    remote_transfer_timeout_sec: int = 300
    read_range_max_cells: int = 20_000
    read_table_max_rows: int = 10_000
    search_result_limit: int = 100
    max_cells_per_update: int = 5_000
    max_rows_per_append: int = 2_000
    idle_timeout_sec: int = 1_800
    hard_timeout_sec: int = 7_200
    local_max_file_size_mb: int = 100
    remote_roundtrip_max_file_size_mb: int = 50
    remote_workbook_max_file_size_mb: int = 25


@dataclass(slots=True)
class ExcelLoggingSettings:
    audit_file: Path


@dataclass(slots=True)
class ExcelSourceProfile:
    source_id: str
    source_type: SourceType
    display_name: str
    enabled: bool = True
    read_only: bool = False
    default_mode: str = "local_workspace"
    workspace_root: Path | None = None
    tenant_id: str | None = None
    site_id: str | None = None
    site_url: str | None = None
    drive_id: str | None = None
    library_name: str | None = None
    auth_profile: str | None = None
    allowed_extensions: tuple[str, ...] = (".xlsx", ".xlsm")
    max_file_size_mb: int = 100
    temp_root: Path | None = None
    backup_dir: Path | None = None
    backup_policy: dict[str, Any] = field(default_factory=dict)
    audit_policy: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "read_only": self.read_only,
            "default_mode": self.default_mode,
            "workspace_root": str(self.workspace_root) if self.workspace_root is not None else None,
            "tenant_id": self.tenant_id,
            "site_id": self.site_id,
            "site_url": self.site_url,
            "drive_id": self.drive_id,
            "library_name": self.library_name,
            "auth_profile": self.auth_profile,
            "allowed_extensions": list(self.allowed_extensions),
            "max_file_size_mb": self.max_file_size_mb,
            "temp_root": str(self.temp_root) if self.temp_root is not None else None,
            "backup_dir": str(self.backup_dir) if self.backup_dir is not None else None,
            "backup_policy": dict(self.backup_policy),
            "audit_policy": dict(self.audit_policy),
        }


@dataclass(slots=True)
class ExcelServerConfig:
    server_name: str = "excel-workspace-mcp"
    sources: tuple[ExcelSourceProfile, ...] = ()
    auth_profiles: tuple[dict[str, Any], ...] = ()
    policies: ExcelPolicySettings = field(default_factory=ExcelPolicySettings)
    limits: ExcelLimitSettings = field(default_factory=ExcelLimitSettings)
    logging: ExcelLoggingSettings | None = None

    @classmethod
    def default(cls, workspace_root: Path) -> ExcelServerConfig:
        audit_dir_env = os.getenv("EXCEL_MCP_AUDIT_DIR")
        backup_dir_env = os.getenv("EXCEL_MCP_BACKUP_DIR")
        backup_dir = _resolve_runtime_path(
            backup_dir_env or ".excel_mcp_backups",
            base_dir=workspace_root,
        )
        audit_dir = _resolve_runtime_path(
            audit_dir_env or ".orchestra_state/audit",
            base_dir=workspace_root,
        )
        source = ExcelSourceProfile(
            source_id="local_workspace",
            source_type="local_workspace",
            display_name="Local Workspace",
            workspace_root=workspace_root,
            default_mode="local_workspace",
            temp_root=_resolve_runtime_path(".excel_mcp_tmp", base_dir=workspace_root),
            backup_dir=backup_dir,
        )
        return cls(
            sources=(source,),
            logging=ExcelLoggingSettings(audit_file=audit_dir / "excel_workspace_mcp.jsonl"),
        )

    def source_map(self) -> dict[str, ExcelSourceProfile]:
        return {source.source_id: source for source in self.sources}


def load_excel_server_config(
    workspace_root: Path,
    config_path: Path | str | None = None,
) -> ExcelServerConfig:
    explicit = Path(config_path).resolve() if config_path is not None else None
    env_path = os.getenv("EXCEL_MCP_CONFIG")
    resolved_path = explicit or (Path(env_path).resolve() if env_path else None)
    if resolved_path is None:
        return ExcelServerConfig.default(workspace_root)
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Excel MCP config file '{resolved_path}' was not found.")

    payload = _load_config_payload(resolved_path)
    if not isinstance(payload, dict):
        raise ValueError("Excel MCP config must decode to an object.")

    base_dir = resolved_path.parent
    policies = _parse_policies(payload.get("policies"))
    limits = _parse_limits(payload.get("limits"))
    logging = _parse_logging(
        payload.get("logging"),
        base_dir=base_dir,
        workspace_root=workspace_root,
    )
    sources = _parse_sources(
        payload.get("sources"),
        base_dir=base_dir,
        workspace_root=workspace_root,
        limits=limits,
    )
    if not sources:
        fallback = ExcelServerConfig.default(workspace_root)
        sources = list(fallback.sources)
        if logging is None:
            logging = fallback.logging

    server_payload = _as_dict(payload.get("server"))
    auth_profiles = _parse_auth_profiles(payload.get("auth_profiles"))
    return ExcelServerConfig(
        server_name=_as_str(server_payload.get("name"), "excel-workspace-mcp"),
        sources=tuple(sources),
        auth_profiles=tuple(auth_profiles),
        policies=policies,
        limits=limits,
        logging=logging
        if logging is not None
        else ExcelServerConfig.default(workspace_root).logging,
    )


def _load_config_payload(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".toml":
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("TOML config must decode to a table.")
        return payload
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "YAML config requires optional dependency 'PyYAML'. "
                "Install with `pip install \"orchestra-agent[mcp-server]\"`."
            ) from exc
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)  # type: ignore[no-any-return]
        if not isinstance(payload, dict):
            raise ValueError("YAML config must decode to a mapping.")
        return payload
    raise ValueError("Excel MCP config must use .toml, .yaml, or .yml.")


def _parse_sources(
    raw_sources: Any,
    *,
    base_dir: Path,
    workspace_root: Path,
    limits: ExcelLimitSettings,
) -> list[ExcelSourceProfile]:
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        raise ValueError("Excel MCP 'sources' must be an array.")

    sources: list[ExcelSourceProfile] = []
    for item in raw_sources:
        payload = _as_dict(item)
        source_type = _as_source_type(payload.get("source_type"), "local_workspace")
        source_workspace = _as_optional_path(
            payload.get("workspace_root"),
            base_dir=base_dir,
            default=workspace_root,
        )
        temp_root = _as_optional_path(
            payload.get("temp_root"),
            base_dir=base_dir,
            default=(source_workspace or workspace_root) / ".excel_mcp_tmp",
        )
        backup_dir = _as_optional_path(
            payload.get("backup_dir"),
            base_dir=base_dir,
            default=(source_workspace or workspace_root) / ".excel_mcp_backups",
        )
        allowed_extensions = _as_extension_list(payload.get("allowed_extensions"))
        sources.append(
            ExcelSourceProfile(
                source_id=_as_str(payload.get("source_id"), "local_workspace"),
                source_type=source_type,
                display_name=_as_str(payload.get("display_name"), "Local Workspace"),
                enabled=_as_bool(payload.get("enabled"), True),
                read_only=_as_bool(payload.get("read_only"), False),
                default_mode=_as_str(payload.get("default_mode"), source_type),
                workspace_root=source_workspace,
                tenant_id=_as_optional_str(payload.get("tenant_id")),
                site_id=_as_optional_str(payload.get("site_id")),
                site_url=_as_optional_str(payload.get("site_url")),
                drive_id=_as_optional_str(payload.get("drive_id")),
                library_name=_as_optional_str(payload.get("library_name")),
                auth_profile=_as_optional_str(payload.get("auth_profile")),
                allowed_extensions=allowed_extensions or (".xlsx", ".xlsm"),
                max_file_size_mb=_as_int(
                    payload.get("max_file_size_mb"),
                    limits.local_max_file_size_mb,
                ),
                temp_root=temp_root,
                backup_dir=backup_dir,
                backup_policy=_as_dict(payload.get("backup_policy")),
                audit_policy=_as_dict(payload.get("audit_policy")),
            )
        )
    return sources


def _parse_policies(raw_policies: Any) -> ExcelPolicySettings:
    payload = _as_dict(raw_policies)
    return ExcelPolicySettings(
        destructive_requires_preview=_as_bool(
            payload.get("destructive_requires_preview"),
            True,
        ),
        commit_requires_validation=_as_bool(
            payload.get("commit_requires_validation"),
            True,
        ),
        allow_delete_sheet=_as_bool(payload.get("allow_delete_sheet"), False),
        allow_formula_write=_as_bool(payload.get("allow_formula_write"), True),
        allow_remote_overwrite=_as_bool(payload.get("allow_remote_overwrite"), False),
        default_remote_mode=_as_str(
            payload.get("default_remote_mode"),
            "sharepoint_graph_roundtrip",
        ),
        selected_scope_required=_as_bool(
            payload.get("selected_scope_required"),
            False,
        ),
        backup_retention_days=_as_int(payload.get("backup_retention_days"), 30),
    )


def _parse_limits(raw_limits: Any) -> ExcelLimitSettings:
    payload = _as_dict(raw_limits)
    return ExcelLimitSettings(
        inspect_workbook_timeout_sec=_as_int(
            payload.get("inspect_workbook_timeout_sec"),
            15,
        ),
        read_range_timeout_sec=_as_int(payload.get("read_range_timeout_sec"), 20),
        preview_edit_timeout_sec=_as_int(
            payload.get("preview_edit_timeout_sec"),
            30,
        ),
        commit_local_timeout_sec=_as_int(payload.get("commit_local_timeout_sec"), 60),
        remote_transfer_timeout_sec=_as_int(
            payload.get("remote_transfer_timeout_sec"),
            300,
        ),
        read_range_max_cells=_as_int(payload.get("read_range_max_cells"), 20_000),
        read_table_max_rows=_as_int(payload.get("read_table_max_rows"), 10_000),
        search_result_limit=_as_int(payload.get("search_result_limit"), 100),
        max_cells_per_update=_as_int(payload.get("max_cells_per_update"), 5_000),
        max_rows_per_append=_as_int(payload.get("max_rows_per_append"), 2_000),
        idle_timeout_sec=_as_int(payload.get("idle_timeout_sec"), 1_800),
        hard_timeout_sec=_as_int(payload.get("hard_timeout_sec"), 7_200),
        local_max_file_size_mb=_as_int(payload.get("local_max_file_size_mb"), 100),
        remote_roundtrip_max_file_size_mb=_as_int(
            payload.get("remote_roundtrip_max_file_size_mb"),
            50,
        ),
        remote_workbook_max_file_size_mb=_as_int(
            payload.get("remote_workbook_max_file_size_mb"),
            25,
        ),
    )


def _parse_logging(
    raw_logging: Any,
    *,
    base_dir: Path,
    workspace_root: Path,
) -> ExcelLoggingSettings | None:
    payload = _as_dict(raw_logging)
    audit_dir_env = os.getenv("EXCEL_MCP_AUDIT_DIR")
    if audit_dir_env:
        return ExcelLoggingSettings(
            audit_file=_resolve_runtime_path(audit_dir_env, base_dir=workspace_root)
            / "excel_workspace_mcp.jsonl"
        )
    if not payload:
        return None
    audit_file = _as_optional_path(
        payload.get("audit_file"),
        base_dir=base_dir,
        default=workspace_root / ".orchestra_state" / "audit" / "excel_workspace_mcp.jsonl",
    )
    if audit_file is None:
        return None
    return ExcelLoggingSettings(audit_file=audit_file)


def _parse_auth_profiles(raw_auth_profiles: Any) -> list[dict[str, Any]]:
    if raw_auth_profiles is None:
        return []
    if not isinstance(raw_auth_profiles, list):
        raise ValueError("Excel MCP 'auth_profiles' must be an array.")
    return [_as_dict(item) for item in raw_auth_profiles]


def _resolve_runtime_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(raw_path)
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Excel MCP config sections must be mappings.")
    return value


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("Excel MCP config value must be a string.")
    stripped = value.strip()
    if not stripped:
        return default
    return stripped


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Excel MCP config value must be a string or null.")
    stripped = value.strip()
    return stripped or None


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("Excel MCP config value must be a boolean.")
    return value


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Excel MCP config value must be an integer.")
    return value


def _as_optional_path(
    value: Any,
    *,
    base_dir: Path,
    default: Path | None = None,
) -> Path | None:
    if value is None:
        return default.resolve() if default is not None else None
    if not isinstance(value, str):
        raise ValueError("Excel MCP path config value must be a string.")
    return _resolve_runtime_path(value, base_dir=base_dir)


def _as_extension_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("allowed_extensions must be an array of strings.")
    normalized = []
    for item in value:
        stripped = item.strip().lower()
        if not stripped:
            continue
        normalized.append(stripped if stripped.startswith(".") else f".{stripped}")
    return tuple(normalized)


def _as_source_type(value: Any, default: SourceType) -> SourceType:
    if value is None:
        return default
    allowed = {
        "local_workspace",
        "sharepoint_graph_workbook",
        "sharepoint_graph_roundtrip",
        "onedrive_graph_workbook",
        "onedrive_graph_roundtrip",
    }
    if value not in allowed:
        raise ValueError(f"Unsupported Excel source_type '{value}'.")
    return cast(SourceType, value)
