from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

type FileSourceType = Literal[
    "local_workspace",
    "sharepoint_drive",
    "onedrive_business",
    "sharepoint_library_manifest",
    "onedrive_manifest",
]
type FileSearchMode = Literal[
    "direct_path_only",
    "path_prefix_walk",
    "graph_search",
    "manifest_only",
]
type FileWriteMode = Literal[
    "local_native",
    "remote_roundtrip",
    "remote_direct_upload_only",
]


@dataclass(slots=True)
class FilePolicySettings:
    destructive_requires_preview: bool = True
    commit_requires_validation: bool = True
    auto_backup: bool = True
    binary_write_restricted: bool = True
    delete_enabled: bool = False
    remote_overwrite_enabled: bool = False
    max_regex_replace_count: int = 1000
    local_backup_retention_days: int = 14
    remote_snapshot_retention_days: int = 7
    audit_retention_days: int = 90


@dataclass(slots=True)
class FileLimitSettings:
    max_text_read_chars: int = 200_000
    max_document_extract_chars: int = 100_000
    max_inline_write_chars: int = 500_000
    max_file_size_mb_local: int = 100
    max_file_size_mb_remote_roundtrip: int = 50
    max_search_results: int = 100
    max_children_list: int = 1000
    small_upload_threshold_mb: int = 4
    idle_timeout_sec: int = 1800
    hard_timeout_sec: int = 7200


@dataclass(slots=True)
class FileLoggingSettings:
    audit_file: Path


@dataclass(slots=True)
class ManifestAlias:
    alias: str
    source_id: str
    base_folder_path: str
    read_only: bool = False
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "source_id": self.source_id,
            "base_folder_path": self.base_folder_path,
            "read_only": self.read_only,
            "tags": list(self.tags),
        }


@dataclass(slots=True)
class FileSourceProfile:
    source_id: str
    source_type: FileSourceType
    display_name: str
    enabled: bool = True
    read_only: bool = False
    workspace_root: Path | None = None
    tenant_id: str | None = None
    site_id: str | None = None
    site_url: str | None = None
    drive_id: str | None = None
    library_name: str | None = None
    auth_profile: str | None = None
    search_mode: FileSearchMode = "path_prefix_walk"
    write_mode: FileWriteMode = "local_native"
    allowed_extensions: tuple[str, ...] = ()
    denied_extensions: tuple[str, ...] = ()
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
            "workspace_root": str(self.workspace_root) if self.workspace_root is not None else None,
            "tenant_id": self.tenant_id,
            "site_id": self.site_id,
            "site_url": self.site_url,
            "drive_id": self.drive_id,
            "library_name": self.library_name,
            "auth_profile": self.auth_profile,
            "search_mode": self.search_mode,
            "write_mode": self.write_mode,
            "allowed_extensions": list(self.allowed_extensions),
            "denied_extensions": list(self.denied_extensions),
            "temp_root": str(self.temp_root) if self.temp_root is not None else None,
            "backup_dir": str(self.backup_dir) if self.backup_dir is not None else None,
            "backup_policy": dict(self.backup_policy),
            "audit_policy": dict(self.audit_policy),
        }


@dataclass(slots=True)
class FileServerConfig:
    server_name: str = "file-workspace-mcp"
    sources: tuple[FileSourceProfile, ...] = ()
    auth_profiles: tuple[dict[str, Any], ...] = ()
    aliases: tuple[ManifestAlias, ...] = ()
    policies: FilePolicySettings = field(default_factory=FilePolicySettings)
    limits: FileLimitSettings = field(default_factory=FileLimitSettings)
    logging: FileLoggingSettings | None = None

    @classmethod
    def default(cls, workspace_root: Path) -> FileServerConfig:
        backup_root = _resolve_runtime_path(
            os.getenv("FILE_MCP_BACKUP_DIR") or ".file_mcp_backups",
            base_dir=workspace_root,
        )
        audit_root = _resolve_runtime_path(
            os.getenv("FILE_MCP_AUDIT_DIR") or ".orchestra_state/audit",
            base_dir=workspace_root,
        )
        source = FileSourceProfile(
            source_id="local_workspace",
            source_type="local_workspace",
            display_name="Local Workspace",
            enabled=True,
            read_only=False,
            workspace_root=workspace_root,
            search_mode="path_prefix_walk",
            write_mode="local_native",
            allowed_extensions=DEFAULT_ALLOWED_EXTENSIONS,
            denied_extensions=DEFAULT_DENIED_EXTENSIONS,
            temp_root=_resolve_runtime_path(".file_mcp_tmp", base_dir=workspace_root),
            backup_dir=backup_root,
        )
        return cls(
            sources=(source,),
            logging=FileLoggingSettings(audit_file=audit_root / "file_workspace_mcp.jsonl"),
        )


def load_file_server_config(
    workspace_root: Path,
    config_path: Path | str | None = None,
) -> FileServerConfig:
    explicit = Path(config_path).resolve() if config_path is not None else None
    env_path = os.getenv("FILE_MCP_CONFIG")
    resolved_path = explicit or (Path(env_path).resolve() if env_path else None)
    if resolved_path is None:
        return FileServerConfig.default(workspace_root)
    if not resolved_path.is_file():
        raise FileNotFoundError(f"File MCP config file '{resolved_path}' was not found.")

    payload = _load_config_payload(resolved_path)
    if not isinstance(payload, dict):
        raise ValueError("File MCP config must decode to an object.")

    base_dir = resolved_path.parent
    policies = _parse_policies(payload.get("policies"))
    limits = _parse_limits(payload.get("limits"))
    logging = _parse_logging(
        payload.get("logging"), base_dir=base_dir, workspace_root=workspace_root
    )
    sources = _parse_sources(
        payload.get("sources"), base_dir=base_dir, workspace_root=workspace_root
    )
    aliases = _parse_aliases(payload.get("aliases"))
    auth_profiles = _parse_auth_profiles(payload.get("auth_profiles"))
    server_payload = _as_dict(payload.get("server"))
    if not sources:
        fallback = FileServerConfig.default(workspace_root)
        sources = list(fallback.sources)
        if logging is None:
            logging = fallback.logging
    return FileServerConfig(
        server_name=_as_str(server_payload.get("name"), "file-workspace-mcp"),
        sources=tuple(sources),
        auth_profiles=tuple(auth_profiles),
        aliases=tuple(aliases),
        policies=policies,
        limits=limits,
        logging=logging
        if logging is not None
        else FileServerConfig.default(workspace_root).logging,
    )


DEFAULT_TEXT_EXTENSIONS: tuple[str, ...] = (
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".jsonc",
    ".yaml",
    ".yml",
    ".xml",
    ".ini",
    ".cfg",
    ".toml",
    ".ps1",
    ".sh",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".cs",
    ".vb",
    ".java",
    ".go",
    ".rs",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".log",
)
DEFAULT_DOCUMENT_EXTENSIONS: tuple[str, ...] = (".pdf", ".docx", ".pptx", ".rtf", ".odt")
DEFAULT_BINARY_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".zip",
    ".7z",
    ".exe",
    ".dll",
    ".so",
    ".pfx",
    ".cer",
    ".p12",
    ".xlsx",
    ".xlsm",
)
DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    *DEFAULT_TEXT_EXTENSIONS,
    *DEFAULT_DOCUMENT_EXTENSIONS,
    *DEFAULT_BINARY_EXTENSIONS,
)
DEFAULT_DENIED_EXTENSIONS: tuple[str, ...] = (".key", ".pem", ".ppk", ".kdbx")


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
                'Install with `pip install "orchestra-agent[mcp-server]"`.'
            ) from exc
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)  # type: ignore[no-any-return]
        if not isinstance(payload, dict):
            raise ValueError("YAML config must decode to a mapping.")
        return payload
    raise ValueError("File MCP config must use .toml, .yaml, or .yml.")


def _parse_sources(
    raw_sources: Any,
    *,
    base_dir: Path,
    workspace_root: Path,
) -> list[FileSourceProfile]:
    if raw_sources is None:
        return []
    if not isinstance(raw_sources, list):
        raise ValueError("File MCP 'sources' must be an array.")

    sources: list[FileSourceProfile] = []
    for item in raw_sources:
        payload = _as_dict(item)
        source_type = _as_source_type(payload.get("source_type"), "local_workspace")
        workspace = _as_optional_path(
            payload.get("workspace_root"),
            base_dir=base_dir,
            default=workspace_root if source_type == "local_workspace" else None,
        )
        write_mode = _as_write_mode(
            payload.get("write_mode"),
            "local_native" if source_type == "local_workspace" else "remote_roundtrip",
        )
        search_mode = _as_search_mode(
            payload.get("search_mode"),
            "path_prefix_walk" if source_type == "local_workspace" else "manifest_only",
        )
        sources.append(
            FileSourceProfile(
                source_id=_as_str(payload.get("source_id"), "local_workspace"),
                source_type=source_type,
                display_name=_as_str(payload.get("display_name"), "Local Workspace"),
                enabled=_as_bool(payload.get("enabled"), True),
                read_only=_as_bool(payload.get("read_only"), False),
                workspace_root=workspace,
                tenant_id=_as_optional_str(payload.get("tenant_id")),
                site_id=_as_optional_str(payload.get("site_id")),
                site_url=_as_optional_str(payload.get("site_url")),
                drive_id=_as_optional_str(payload.get("drive_id")),
                library_name=_as_optional_str(payload.get("library_name")),
                auth_profile=_as_optional_str(payload.get("auth_profile")),
                search_mode=search_mode,
                write_mode=write_mode,
                allowed_extensions=_as_extension_list(payload.get("allowed_extensions"))
                or DEFAULT_ALLOWED_EXTENSIONS,
                denied_extensions=_as_extension_list(payload.get("denied_extensions"))
                or DEFAULT_DENIED_EXTENSIONS,
                temp_root=_as_optional_path(
                    payload.get("temp_root"),
                    base_dir=base_dir,
                    default=(workspace or workspace_root) / ".file_mcp_tmp",
                ),
                backup_dir=_as_optional_path(
                    payload.get("backup_dir"),
                    base_dir=base_dir,
                    default=(workspace or workspace_root) / ".file_mcp_backups",
                ),
                backup_policy=_as_dict(payload.get("backup_policy")),
                audit_policy=_as_dict(payload.get("audit_policy")),
            )
        )
    return sources


def _parse_policies(raw_policies: Any) -> FilePolicySettings:
    payload = _as_dict(raw_policies)
    return FilePolicySettings(
        destructive_requires_preview=_as_bool(
            payload.get("destructive_requires_preview"),
            True,
        ),
        commit_requires_validation=_as_bool(
            payload.get("commit_requires_validation"),
            True,
        ),
        auto_backup=_as_bool(payload.get("auto_backup"), True),
        binary_write_restricted=_as_bool(payload.get("binary_write_restricted"), True),
        delete_enabled=_as_bool(payload.get("delete_enabled"), False),
        remote_overwrite_enabled=_as_bool(
            payload.get("remote_overwrite_enabled", payload.get("allow_remote_overwrite")),
            False,
        ),
        max_regex_replace_count=_as_int(payload.get("max_regex_replace_count"), 1000),
        local_backup_retention_days=_as_int(
            payload.get("local_backup_retention_days"),
            14,
        ),
        remote_snapshot_retention_days=_as_int(
            payload.get("remote_snapshot_retention_days"),
            7,
        ),
        audit_retention_days=_as_int(payload.get("audit_retention_days"), 90),
    )


def _parse_limits(raw_limits: Any) -> FileLimitSettings:
    payload = _as_dict(raw_limits)
    return FileLimitSettings(
        max_text_read_chars=_as_int(payload.get("max_text_read_chars"), 200_000),
        max_document_extract_chars=_as_int(
            payload.get("max_document_extract_chars"),
            100_000,
        ),
        max_inline_write_chars=_as_int(payload.get("max_inline_write_chars"), 500_000),
        max_file_size_mb_local=_as_int(payload.get("max_file_size_mb_local"), 100),
        max_file_size_mb_remote_roundtrip=_as_int(
            payload.get("max_file_size_mb_remote_roundtrip"),
            50,
        ),
        max_search_results=_as_int(payload.get("max_search_results"), 100),
        max_children_list=_as_int(payload.get("max_children_list"), 1000),
        small_upload_threshold_mb=_as_int(payload.get("small_upload_threshold_mb"), 4),
        idle_timeout_sec=_as_int(payload.get("idle_timeout_sec"), 1800),
        hard_timeout_sec=_as_int(payload.get("hard_timeout_sec"), 7200),
    )


def _parse_logging(
    raw_logging: Any,
    *,
    base_dir: Path,
    workspace_root: Path,
) -> FileLoggingSettings | None:
    payload = _as_dict(raw_logging)
    audit_dir_env = os.getenv("FILE_MCP_AUDIT_DIR")
    if audit_dir_env:
        return FileLoggingSettings(
            audit_file=_resolve_runtime_path(audit_dir_env, base_dir=workspace_root)
            / "file_workspace_mcp.jsonl"
        )
    if not payload:
        return None
    audit_file = _as_optional_path(
        payload.get("audit_file"),
        base_dir=base_dir,
        default=workspace_root / ".orchestra_state" / "audit" / "file_workspace_mcp.jsonl",
    )
    if audit_file is None:
        return None
    return FileLoggingSettings(audit_file=audit_file)


def _parse_aliases(raw_aliases: Any) -> list[ManifestAlias]:
    if raw_aliases is None:
        return []
    if not isinstance(raw_aliases, list):
        raise ValueError("File MCP 'aliases' must be an array.")
    aliases: list[ManifestAlias] = []
    for item in raw_aliases:
        payload = _as_dict(item)
        aliases.append(
            ManifestAlias(
                alias=_as_str(payload.get("alias"), ""),
                source_id=_as_str(payload.get("source_id"), ""),
                base_folder_path=_as_str(payload.get("base_folder_path"), ""),
                read_only=_as_bool(payload.get("read_only"), False),
                tags=tuple(_as_str_list(payload.get("tags"))),
            )
        )
    return aliases


def _parse_auth_profiles(raw_auth_profiles: Any) -> list[dict[str, Any]]:
    if raw_auth_profiles is None:
        return []
    if not isinstance(raw_auth_profiles, list):
        raise ValueError("File MCP 'auth_profiles' must be an array.")
    return [_as_dict(item) for item in raw_auth_profiles]


def _resolve_runtime_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(raw_path)
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("File MCP config sections must be mappings.")
    return value


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("File MCP config value must be a string.")
    stripped = _expand_env_placeholders(value.strip())
    return stripped or default


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("File MCP config value must be a string or null.")
    stripped = _expand_env_placeholders(value.strip())
    return stripped or None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("File MCP config value must be an array of strings.")
    return [_expand_env_placeholders(item.strip()) for item in value if item.strip()]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("File MCP config value must be a boolean.")
    return value


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("File MCP config value must be an integer.")
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
        raise ValueError("File MCP path config value must be a string.")
    return _resolve_runtime_path(_expand_env_placeholders(value), base_dir=base_dir)


def _as_extension_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Extension config must be an array of strings.")
    normalized = []
    for item in value:
        lowered = _expand_env_placeholders(item.strip()).lower()
        if not lowered:
            continue
        normalized.append(lowered if lowered.startswith(".") else f".{lowered}")
    return tuple(normalized)


_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_placeholders(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.getenv(name, match.group(0))

    return _ENV_PLACEHOLDER.sub(replace, value)


def _as_source_type(value: Any, default: FileSourceType) -> FileSourceType:
    if value is None:
        return default
    allowed = {
        "local_workspace",
        "sharepoint_drive",
        "onedrive_business",
        "sharepoint_library_manifest",
        "onedrive_manifest",
    }
    if value not in allowed:
        raise ValueError(f"Unsupported file source_type '{value}'.")
    return cast(FileSourceType, value)


def _as_search_mode(value: Any, default: FileSearchMode) -> FileSearchMode:
    if value is None:
        return default
    allowed = {"direct_path_only", "path_prefix_walk", "graph_search", "manifest_only"}
    if value not in allowed:
        raise ValueError(f"Unsupported file search_mode '{value}'.")
    return cast(FileSearchMode, value)


def _as_write_mode(value: Any, default: FileWriteMode) -> FileWriteMode:
    if value is None:
        return default
    allowed = {"local_native", "remote_roundtrip", "remote_direct_upload_only"}
    if value not in allowed:
        raise ValueError(f"Unsupported file write_mode '{value}'.")
    return cast(FileWriteMode, value)
