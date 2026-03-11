from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.llm_client import LlmAttachment
from orchestra_agent.ports.mcp_client import IMcpClient
from orchestra_agent.shared.mcp_tool_catalog import normalize_mcp_tool_catalog


@dataclass(slots=True, frozen=True)
class WorkspaceIndexSnapshot:
    indexed_files: tuple[dict[str, Any], ...]
    indexed_paths: frozenset[str]

    def files_for_prompt(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.indexed_files]


class WorkspaceFileInventory:
    def __init__(
        self,
        *,
        workspace_root: Path,
        max_files: int,
        ignored_dirs: set[str] | frozenset[str],
        path_resolver: Callable[[str], Path],
    ) -> None:
        self._workspace_root = workspace_root
        self._max_files = max_files
        self._ignored_dirs = frozenset(ignored_dirs)
        self._path_resolver = path_resolver
        self._cache_revision = 0
        self._snapshot_cache: dict[tuple[str, ...], tuple[int, WorkspaceIndexSnapshot]] = {}

    def snapshot(self, resolved_input: dict[str, Any]) -> WorkspaceIndexSnapshot:
        roots = self._discovery_roots(resolved_input)
        cache_key = tuple(str(root) for root in roots)
        cached = self._snapshot_cache.get(cache_key)
        if cached is not None and cached[0] == self._cache_revision:
            return cached[1]

        snapshot = self._build_snapshot(roots)
        self._snapshot_cache[cache_key] = (self._cache_revision, snapshot)
        return snapshot

    def invalidate(self) -> None:
        self._cache_revision += 1
        self._snapshot_cache.clear()

    def _discovery_roots(self, resolved_input: dict[str, Any]) -> tuple[Path, ...]:
        raw_roots = resolved_input.get("llm_file_discovery_roots")
        if isinstance(raw_roots, list) and all(isinstance(item, str) for item in raw_roots):
            return tuple(self._path_resolver(item) for item in raw_roots)
        return (self._workspace_root,)

    def _build_snapshot(self, roots: tuple[Path, ...]) -> WorkspaceIndexSnapshot:
        indexed_files: list[dict[str, Any]] = []
        indexed_paths: set[str] = set()
        self._scan_roots(
            roots=roots,
            indexed_files=indexed_files,
            indexed_paths=indexed_paths,
        )
        return WorkspaceIndexSnapshot(
            indexed_files=tuple(indexed_files),
            indexed_paths=frozenset(indexed_paths),
        )

    def _scan_roots(
        self,
        *,
        roots: tuple[Path, ...],
        indexed_files: list[dict[str, Any]],
        indexed_paths: set[str],
    ) -> None:
        for root in roots:
            if len(indexed_files) >= self._max_files:
                return
            if root.is_file():
                self._append_file(root, indexed_files=indexed_files, indexed_paths=indexed_paths)
                continue
            if not root.exists():
                continue
            for current_dir, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    dirname
                    for dirname in sorted(dirnames)
                    if dirname not in self._ignored_dirs
                ]
                current_root = Path(current_dir)
                for filename in sorted(filenames):
                    if len(indexed_files) >= self._max_files:
                        return
                    self._append_file(
                        current_root / filename,
                        indexed_files=indexed_files,
                        indexed_paths=indexed_paths,
                    )

    def _append_file(
        self,
        file_path: Path,
        *,
        indexed_files: list[dict[str, Any]],
        indexed_paths: set[str],
    ) -> None:
        try:
            relative_path = file_path.relative_to(self._workspace_root).as_posix()
        except ValueError:
            return
        if relative_path in indexed_paths:
            return
        try:
            size_bytes = file_path.stat().st_size
        except OSError:
            return
        indexed_paths.add(relative_path)
        indexed_files.append({"path": relative_path, "size": size_bytes})


class WorkspacePathManager:
    def __init__(
        self,
        *,
        workspace_root: Path,
        path_value_keys: set[str] | frozenset[str],
        path_list_keys: set[str] | frozenset[str],
    ) -> None:
        self._workspace_root = workspace_root
        self._path_value_keys = frozenset(path_value_keys)
        self._path_list_keys = frozenset(path_list_keys)

    def workflow_attachments(
        self,
        workflow: Workflow,
        resolved_input: dict[str, Any],
    ) -> tuple[LlmAttachment, ...]:
        raw_files = [*workflow.reference_files]
        extra_files = resolved_input.get("llm_reference_files")
        if isinstance(extra_files, list) and all(isinstance(item, str) for item in extra_files):
            raw_files.extend(extra_files)

        seen: set[str] = set()
        attachments: list[LlmAttachment] = []
        for raw_file in raw_files:
            resolved = self.resolve_attachment_path(raw_file)
            normalized = str(resolved)
            if normalized in seen:
                continue
            seen.add(normalized)
            attachments.append(LlmAttachment(path=normalized))
        return tuple(attachments)

    def append_requested_attachments(
        self,
        *,
        request_actions: list[dict[str, Any]],
        indexed_paths: set[str],
        attached_files: list[LlmAttachment],
    ) -> list[str]:
        existing_paths = {attachment.path for attachment in attached_files}
        newly_requested: list[str] = []

        for action in request_actions:
            paths = action.get("paths", [])
            if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
                raise ValueError("request_file_attachments requires a string array 'paths'.")
            for raw_path in paths:
                normalized_path = raw_path.replace("\\", "/")
                self._validate_requested_path(normalized_path, raw_path, indexed_paths)
                resolved = self.resolve_attachment_path(normalized_path)
                attachment_path = str(resolved)
                if attachment_path in existing_paths:
                    continue
                existing_paths.add(attachment_path)
                attached_files.append(LlmAttachment(path=attachment_path))
                newly_requested.append(normalized_path)

        return newly_requested

    def sanitize_for_llm(self, value: Any, *, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {
                item_key: self.sanitize_for_llm(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.sanitize_for_llm(item, key=key) for item in value]
        if isinstance(value, str):
            return self._sanitize_string_for_llm(value, key=key)
        return value

    def resolve_workspace_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._workspace_root):
            raise ValueError(f"Workspace sandbox rejected path outside workspace: {raw_path}")
        return resolved

    def resolve_attachment_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute() or candidate.exists():
            resolved = candidate.resolve()
        else:
            resolved = (self._workspace_root / candidate).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"LLM attachment '{resolved}' was not found.")
        return resolved

    def _sanitize_string_for_llm(self, value: str, *, key: str | None = None) -> str:
        if key in self._path_list_keys or key in self._path_value_keys:
            return self.display_path_for_llm(value)
        return value

    def _validate_requested_path(
        self,
        normalized_path: str,
        raw_path: str,
        indexed_paths: set[str],
    ) -> None:
        if normalized_path in indexed_paths:
            return
        raise ValueError(
            f"Requested attachment '{raw_path}' is not available in workspace_file_index."
        )

    def display_path_for_llm(self, raw_path: str) -> str:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved.is_relative_to(self._workspace_root):
                return resolved.relative_to(self._workspace_root).as_posix()
            return resolved.as_posix()
        if "\\" in raw_path:
            return raw_path.replace("\\", "/")
        return raw_path


class McpToolCatalogResolver:
    def __init__(self) -> None:
        self._catalog_cache: dict[int, tuple[object, tuple[dict[str, Any], ...]]] = {}

    def resolve(
        self,
        mcp_client: IMcpClient,
        resolved_input: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str | None]:
        warning: str | None = None
        try:
            tool_catalog = self.describe_tools(mcp_client)
        except Exception as exc:  # noqa: BLE001
            tool_catalog = []
            warning = f"MCP tool catalog unavailable during step execution: {exc}"
        return self._apply_override(tool_catalog, resolved_input), warning

    def describe_tools(self, mcp_client: IMcpClient) -> list[dict[str, Any]]:
        cache_key = id(mcp_client)
        cached = self._catalog_cache.get(cache_key)
        if cached is not None and cached[0] is mcp_client:
            return [dict(tool) for tool in cached[1]]

        described_tools = self._discover_tools(mcp_client)
        cached_tools = tuple(dict(tool) for tool in described_tools)
        self._catalog_cache[cache_key] = (mcp_client, cached_tools)
        return [dict(tool) for tool in cached_tools]

    @staticmethod
    def _discover_tools(mcp_client: IMcpClient) -> list[dict[str, Any]]:
        describe_tools = getattr(mcp_client, "describe_tools", None)
        if callable(describe_tools):
            described_tools = normalize_mcp_tool_catalog(describe_tools())
            if described_tools:
                return sorted(described_tools, key=lambda item: item["name"])

        return sorted(
            normalize_mcp_tool_catalog(mcp_client.list_tools()),
            key=lambda item: item["name"],
        )

    @staticmethod
    def _apply_override(
        tool_catalog: list[dict[str, Any]],
        resolved_input: dict[str, Any],
    ) -> list[dict[str, Any]]:
        override = resolved_input.get("allowed_mcp_tools")
        if not (isinstance(override, list) and all(isinstance(item, str) for item in override)):
            return tool_catalog

        override_set = set(override)
        filtered_catalog = [tool for tool in tool_catalog if tool["name"] in override_set]
        known_tools = {tool["name"] for tool in filtered_catalog}
        for tool_name in sorted(override_set - known_tools):
            filtered_catalog.append({"name": tool_name, "description": ""})
        return filtered_catalog
