from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import os
import re
import shutil
import time as time_module
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4
from zipfile import ZipFile

import httpx

from orchestra_agent.mcp_server.file_config import (
    DEFAULT_DENIED_EXTENSIONS,
    DEFAULT_DOCUMENT_EXTENSIONS,
    DEFAULT_TEXT_EXTENSIONS,
    FileServerConfig,
    FileSourceProfile,
    ManifestAlias,
    load_file_server_config,
)
from orchestra_agent.mcp_server.file_graph_client import (
    GraphDriveItem,
    GraphFileClient,
    GraphFileClientError,
)
from orchestra_agent.mcp_server.logging_utils import get_mcp_logger, log_event

logger = get_mcp_logger(__name__)

type SessionState = Literal[
    "CREATED",
    "STAGING",
    "PREVIEWED",
    "VALIDATED",
    "COMMITTED",
    "CANCELED",
    "FAILED",
    "EXPIRED",
]
type ItemType = Literal["file", "folder"]
type TargetMode = Literal["local", "remote_roundtrip"]

_SECRET_NAME_PATTERNS = (
    re.compile(r"(^|[\\/])\.env($|\.)", re.IGNORECASE),
    re.compile(r"(^|[\\/])secrets?\.", re.IGNORECASE),
    re.compile(r"(^|[\\/])credentials?\.", re.IGNORECASE),
    re.compile(r"(^|[\\/])id_rsa$", re.IGNORECASE),
)
_INTERNAL_ENTRY_NAMES = {
    ".file_mcp_backups",
    ".file_mcp_tmp",
    ".orchestra_state",
}


class FileToolError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
        retriable: bool = False,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})
        self.retriable = retriable
        self.suggested_action = suggested_action

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "detail": dict(self.detail),
            "retriable": self.retriable,
        }
        if self.suggested_action is not None:
            payload["suggested_action"] = self.suggested_action
        return payload

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class FileStagedOperation:
    operation_id: str
    operation_type: str
    target_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    before_text: str | None = None
    after_text: str | None = None


@dataclass(slots=True)
class FileEditSession:
    session_id: str
    source_id: str
    target_item_ref: dict[str, Any]
    target_mode: TargetMode
    base_hash: str | None
    base_etag: str | None
    base_size: int | None
    base_modified_at: str | None
    temp_local_copy: Path | None
    opened_at: datetime
    expires_at: datetime
    actor: str
    staged_operations: list[FileStagedOperation] = field(default_factory=list)
    preview_summary: dict[str, Any] | None = None
    validation_summary: dict[str, Any] | None = None
    backup_ref: dict[str, Any] | None = None
    commit_result: dict[str, Any] | None = None
    audit_ref: str | None = None
    state: SessionState = "CREATED"
    initial_exists: bool = True
    item_type: ItemType = "file"
    metadata: dict[str, Any] = field(default_factory=dict)
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.last_accessed_at = datetime.now(UTC)


class WorkspaceFileService:
    def __init__(
        self,
        workspace_root: Path,
        max_read_bytes: int = 1_000_000,
        config: FileServerConfig | None = None,
        graph_transport: httpx.BaseTransport | None = None,
        graph_base_url: str = "https://graph.microsoft.com/v1.0",
    ) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be greater than zero.")
        self._workspace_root = Path(workspace_root).resolve()
        self._max_read_bytes = max_read_bytes
        self._config = config or load_file_server_config(self._workspace_root)
        self._sources = self._normalize_sources(self._config.sources)
        self._aliases = self._normalize_aliases(self._config.aliases)
        self._auth_profiles = self._normalize_auth_profiles(self._config.auth_profiles)
        self._sessions: dict[str, FileEditSession] = {}
        self._graph_transport = graph_transport
        self._graph_base_url = graph_base_url
        self._remote_clients: dict[str, GraphFileClient] = {}
        self._remote_enabled = _env_flag("FILE_MCP_REMOTE_ENABLED")
        self._audit_file = self._resolve_inside_workspace(
            self._config.logging.audit_file
            if self._config.logging is not None
            else self._workspace_root / ".orchestra_state" / "audit" / "file_workspace_mcp.jsonl"
        )
        self._audit_file.parent.mkdir(parents=True, exist_ok=True)
        log_event(
            logger,
            "file_service_initialized",
            workspace_root=self._workspace_root,
            max_read_bytes=max_read_bytes,
            source_count=len(self._sources),
            alias_count=len(self._aliases),
            auth_profile_count=len(self._auth_profiles),
            audit_file=self._audit_file,
        )

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def list_sources(self, include_disabled: bool = False) -> dict[str, Any]:
        started = time_module.perf_counter()
        sources = [
            source.to_public_dict()
            for source in self._sources.values()
            if include_disabled or source.enabled
        ]
        result = {"sources": sources}
        self._audit("list_sources", "success", duration_ms=_duration_ms(started))
        return result

    def find_items(
        self,
        source_id: str,
        query: str = "",
        *,
        parent: Mapping[str, Any] | str | None = None,
        recursive: bool = True,
        item_types: Sequence[str] | None = None,
        extension_filter: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        source = self._require_source(source_id)
        effective_limit = limit if limit is not None else self._config.limits.max_search_results
        if effective_limit <= 0:
            self._raise_error("VALIDATION_FAILED", "limit must be greater than zero.")
        requested_types = set(item_types or ["file", "folder"])
        normalized_extensions = {_normalize_extension(item) for item in (extension_filter or [])}
        if self._source_is_remote(source):
            parent_paths = self._remote_search_base_paths(source, parent=parent)
            remote_items = self._remote_client(source).find_items(
                query=query,
                base_paths=parent_paths,
                recursive=recursive,
                item_types=requested_types,
                extension_filter=normalized_extensions,
                limit=effective_limit,
            )
            items = [
                {
                    "item_ref": item.to_item_ref(),
                    "display_name": item.name,
                    "location_summary": item.remote_path,
                    "size": item.size,
                    "modified_at": item.modified_at,
                    "matched_reason": self._matched_reason(query, item.name, item.remote_path),
                    "preview_excerpt": None,
                }
                for item in remote_items
            ]
            result = {"items": items}
            self._audit(
                "find_items",
                "success",
                source_id=source.source_id,
                target_path_or_item_id=str(parent) if parent is not None else None,
                duration_ms=_duration_ms(started),
            )
            return result
        search_root = self._source_root(source)
        if parent is not None:
            parent_ref = self._resolve_item_input(parent, default_source_id=source_id)
            if parent_ref["item_type"] != "folder":
                self._raise_error("ITEM_TYPE_MISMATCH", "parent must resolve to a folder.")
            search_root = self._path_from_item_ref(parent_ref)
        query_text = query.strip().lower()
        iterator = search_root.rglob("*") if recursive else search_root.glob("*")
        items: list[dict[str, Any]] = []
        for candidate in sorted(iterator, key=lambda item: item.as_posix().lower()):
            item_type = "folder" if candidate.is_dir() else "file"
            if item_type not in requested_types:
                continue
            if (
                candidate.is_file()
                and normalized_extensions
                and candidate.suffix.lower() not in normalized_extensions
            ):
                continue
            relative_path = candidate.relative_to(search_root).as_posix().lower()
            if (
                query_text
                and query_text not in candidate.name.lower()
                and query_text not in relative_path
            ):
                continue
            items.append(
                {
                    "item_ref": self._build_item_ref(source, candidate),
                    "display_name": candidate.name,
                    "location_summary": candidate.relative_to(source.workspace_root).as_posix(),
                    "size": candidate.stat().st_size if candidate.is_file() else None,
                    "modified_at": _mtime_iso(candidate),
                    "matched_reason": self._matched_reason(query, candidate.name, relative_path),
                    "preview_excerpt": None,
                }
            )
            if len(items) >= effective_limit:
                break
        result = {"items": items}
        self._audit(
            "find_items",
            "success",
            source_id=source.source_id,
            target_path_or_item_id=str(parent) if parent is not None else None,
            duration_ms=_duration_ms(started),
        )
        return result

    def resolve_item(  # noqa: C901
        self,
        source_id: str,
        *,
        path: str | None = None,
        alias: str | None = None,
        remote_ref: Mapping[str, Any] | None = None,
        expected_type: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        source = self._require_source(source_id)
        alias_read_only = False
        if alias is not None:
            manifest_alias = self._aliases.get(alias)
            if manifest_alias is None or manifest_alias.source_id != source_id:
                self._raise_error("ITEM_NOT_FOUND", f"Alias '{alias}' was not found.")
            path = manifest_alias.base_folder_path
            alias_read_only = manifest_alias.read_only
        if path is None and remote_ref is not None:
            candidate = remote_ref.get("local_path") or remote_ref.get("remote_path")
            if isinstance(candidate, str) and candidate.strip():
                path = candidate
        if path is None:
            self._raise_error("ITEM_NOT_FOUND", "resolve_item requires path or alias.")
        if self._source_is_remote(source):
            force_item_type = expected_type if expected_type in {"file", "folder"} else None
            remote_item_id = (
                str(remote_ref["remote_item_id"])
                if remote_ref is not None and isinstance(remote_ref.get("remote_item_id"), str)
                else None
            )
            resolved_item = self._remote_client(source).resolve_item(
                remote_path=path,
                item_id=remote_item_id,
                drive_id=(
                    str(remote_ref["drive_id"])
                    if remote_ref is not None and isinstance(remote_ref.get("drive_id"), str)
                    else None
                ),
                allow_missing=allow_missing,
                force_item_type=force_item_type,  # type: ignore[arg-type]
            )
            item_ref = resolved_item.to_item_ref()
            if alias is not None:
                item_ref["manifest_alias"] = alias
                item_ref["read_only"] = alias_read_only
            if expected_type is not None and item_ref["item_type"] != expected_type:
                self._raise_error(
                    "ITEM_TYPE_MISMATCH",
                    f"Expected a {expected_type}, got {item_ref['item_type']}.",
                )
            result = {"item_ref": item_ref}
            self._audit(
                "resolve_item",
                "success",
                source_id=source.source_id,
                target_path_or_item_id=str(item_ref.get("remote_path")),
                duration_ms=_duration_ms(started),
            )
            return result
        item_path = self._resolve_path_inside_root(path, self._source_root(source))
        if not item_path.exists() and not allow_missing:
            self._raise_error("ITEM_NOT_FOUND", f"Item '{path}' does not exist.")
        item_ref = self._build_item_ref(
            source,
            item_path,
            allow_missing=allow_missing,
        )
        if alias is not None:
            item_ref["manifest_alias"] = alias
            item_ref["read_only"] = alias_read_only
        if expected_type is not None and item_ref["item_type"] != expected_type:
            self._raise_error(
                "ITEM_TYPE_MISMATCH",
                f"Expected a {expected_type}, got {item_ref['item_type']}.",
            )
        result = {"item_ref": item_ref}
        self._audit(
            "resolve_item",
            "success",
            source_id=source.source_id,
            target_path_or_item_id=item_ref.get("local_path"),
            duration_ms=_duration_ms(started),
        )
        return result

    def list_children(
        self,
        folder_ref: Mapping[str, Any] | str,
        *,
        recursive: bool = False,
        limit: int | None = None,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        folder_item = self._resolve_item_input(folder_ref)
        if folder_item["item_type"] != "folder":
            self._raise_error("ITEM_TYPE_MISMATCH", "folder_ref must point to a folder.")
        effective_limit = limit if limit is not None else self._config.limits.max_children_list
        if effective_limit <= 0:
            self._raise_error("VALIDATION_FAILED", "limit must be greater than zero.")
        source = self._require_source(str(folder_item["source_id"]))
        if self._source_is_remote(source):
            remote_folder = self._graph_item_from_ref(folder_item)
            children = [
                item.to_item_ref()
                for item in self._remote_client(source).list_children(
                    remote_folder,
                    recursive=recursive,
                    limit=effective_limit,
                    include_hidden=include_hidden,
                )
            ]
            result = {"children": children}
            self._audit(
                "list_children",
                "success",
                source_id=str(folder_item["source_id"]),
                target_path_or_item_id=str(folder_item.get("remote_path")),
                duration_ms=_duration_ms(started),
            )
            return result
        folder_path = self._path_from_item_ref(folder_item)
        iterator = folder_path.rglob("*") if recursive else folder_path.iterdir()
        children: list[dict[str, Any]] = []
        for child in sorted(iterator, key=lambda item: item.as_posix().lower()):
            if not include_hidden and child.name.startswith("."):
                continue
            children.append(
                self._build_item_ref(source, child)
            )
            if len(children) >= effective_limit:
                break
        result = {"children": children}
        self._audit(
            "list_children",
            "success",
            source_id=str(folder_item["source_id"]),
            target_path_or_item_id=str(folder_item.get("local_path")),
            duration_ms=_duration_ms(started),
        )
        return result

    def get_item_metadata(
        self,
        item_ref: Mapping[str, Any] | str,
        *,
        hashes: bool = False,
        permissions_summary: bool = False,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        resolved = self._resolve_item_input(item_ref)
        source = self._require_source(str(resolved["source_id"]))
        if self._source_is_remote(source):
            remote_item = self._graph_item_from_ref(resolved)
            metadata = {
                "name": remote_item.name,
                "path": remote_item.remote_path,
                "item_type": remote_item.item_type,
                "size": remote_item.size,
                "media_type": remote_item.media_type or "application/octet-stream",
                "extension": PurePosixPath(remote_item.remote_path).suffix.lower(),
                "modified_at": remote_item.modified_at,
                "created_at": None,
                "etag": remote_item.etag,
                "hashes": None,
                "permissions_summary": (
                    {
                        "read_only_source": source.read_only,
                        "os_access": "remote",
                    }
                    if permissions_summary
                    else None
                ),
            }
            self._audit(
                "get_item_metadata",
                "success",
                source_id=str(resolved["source_id"]),
                target_path_or_item_id=str(resolved.get("remote_path")),
                item_type=str(resolved["item_type"]),
                duration_ms=_duration_ms(started),
            )
            return metadata
        path = self._path_from_item_ref(resolved)
        stats = path.stat()
        metadata = {
            "name": path.name,
            "path": resolved.get("local_path"),
            "item_type": resolved["item_type"],
            "size": stats.st_size if path.is_file() else None,
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "extension": path.suffix.lower(),
            "modified_at": _mtime_iso(path),
            "created_at": datetime.fromtimestamp(stats.st_ctime, tz=UTC).isoformat(),
            "etag": None,
            "hashes": {"sha256": _sha256_file(path)} if hashes and path.is_file() else None,
            "permissions_summary": (
                {
                    "read_only_source": source.read_only,
                    "os_access": "read-write" if os.access(path, os.W_OK) else "read-only",
                }
                if permissions_summary
                else None
            ),
        }
        self._audit(
            "get_item_metadata",
            "success",
            source_id=str(resolved["source_id"]),
            target_path_or_item_id=str(resolved.get("local_path")),
            item_type=str(resolved["item_type"]),
            duration_ms=_duration_ms(started),
        )
        return metadata

    def read_text_item(
        self,
        item_ref: Mapping[str, Any] | str,
        *,
        encoding: str | None = None,
        max_chars: int | None = None,
        normalize_newlines: bool = True,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        resolved = self._resolve_item_input(item_ref)
        self._ensure_text_read_allowed(resolved)
        source = self._require_source(str(resolved["source_id"]))
        if self._source_is_remote(source):
            raw_bytes = self._remote_client(source).read_bytes(self._graph_item_from_ref(resolved))
            target_identifier = str(resolved.get("remote_path"))
        else:
            path = self._path_from_item_ref(resolved)
            raw_bytes = path.read_bytes()
            target_identifier = str(resolved.get("local_path"))
        decoded, detected_encoding = _decode_text_bytes(raw_bytes, encoding=encoding)
        text = _normalize_text_newlines(decoded) if normalize_newlines else decoded
        effective_max = (
            max_chars if max_chars is not None else self._config.limits.max_text_read_chars
        )
        truncated = len(text) > effective_max
        content = text[:effective_max] if truncated else text
        result = {
            "content": content,
            "encoding": detected_encoding,
            "char_count": len(content),
            "truncated": truncated,
        }
        self._audit(
            "read_text",
            "success",
            source_id=str(resolved["source_id"]),
            target_path_or_item_id=target_identifier,
            item_type="file",
            duration_ms=_duration_ms(started),
            bytes_read=len(raw_bytes),
        )
        return result

    def read_text_chunk(
        self,
        item_ref: Mapping[str, Any] | str,
        *,
        offset: int,
        length: int,
        unit: str = "chars",
        encoding: str | None = None,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        if offset < 0 or length <= 0:
            self._raise_error("VALIDATION_FAILED", "offset must be >= 0 and length must be > 0.")
        resolved = self._resolve_item_input(item_ref)
        self._ensure_text_read_allowed(resolved)
        source = self._require_source(str(resolved["source_id"]))
        if self._source_is_remote(source):
            raw_bytes = self._remote_client(source).read_bytes(self._graph_item_from_ref(resolved))
            target_identifier = str(resolved.get("remote_path"))
        else:
            path = self._path_from_item_ref(resolved)
            raw_bytes = path.read_bytes()
            target_identifier = str(resolved.get("local_path"))
        decoded, detected_encoding = _decode_text_bytes(raw_bytes, encoding=encoding)
        text = _normalize_text_newlines(decoded)
        if unit == "chars":
            chunk = text[offset : offset + length]
            next_offset = offset + len(chunk)
        elif unit == "lines":
            lines = text.splitlines()
            chunk_lines = lines[offset : offset + length]
            chunk = "\n".join(chunk_lines)
            next_offset = offset + len(chunk_lines)
        elif unit == "bytes":
            sliced = raw_bytes[offset : offset + length]
            chunk, _ = _decode_text_bytes(sliced, encoding=detected_encoding)
            next_offset = offset + len(sliced)
        else:
            self._raise_error("VALIDATION_FAILED", f"Unsupported unit '{unit}'.")
        result = {
            "content": chunk,
            "encoding": detected_encoding,
            "offset": offset,
            "length": length,
            "unit": unit,
            "next_offset": next_offset,
        }
        self._audit(
            "read_text_chunk",
            "success",
            source_id=str(resolved["source_id"]),
            target_path_or_item_id=target_identifier,
            item_type="file",
            duration_ms=_duration_ms(started),
            bytes_read=len(raw_bytes),
        )
        return result

    def extract_document_text(
        self,
        item_ref: Mapping[str, Any] | str,
        *,
        max_chars: int | None = None,
        extraction_mode: str = "text_only",
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        resolved = self._resolve_item_input(item_ref)
        self._ensure_document_read_allowed(resolved)
        source = self._require_source(str(resolved["source_id"]))
        cleanup_dir: Path | None = None
        if self._source_is_remote(source):
            path, cleanup_dir = self._download_remote_item_to_temp(source, resolved)
            target_identifier = str(resolved.get("remote_path"))
        else:
            path = self._path_from_item_ref(resolved)
            target_identifier = str(resolved.get("local_path"))
        extension = path.suffix.lower()
        if extension == ".docx":
            text = _extract_docx_text(path)
        elif extension == ".pptx":
            text = _extract_pptx_text(path)
        elif extension == ".pdf":
            text = _extract_pdf_text(path)
        elif extension == ".rtf":
            text = _extract_rtf_text(path.read_text(encoding="utf-8", errors="ignore"))
        elif extension == ".odt":
            text = _extract_odt_text(path)
        else:
            self._raise_error(
                "ITEM_TYPE_MISMATCH",
                f"Extension '{extension}' is not supported for document extraction.",
            )
        if extraction_mode == "first_pages_only":
            text = text[: min(len(text), 5000)]
        effective_max = (
            max_chars if max_chars is not None else self._config.limits.max_document_extract_chars
        )
        truncated = len(text) > effective_max
        content = text[:effective_max] if truncated else text
        result = {
            "content": content,
            "char_count": len(content),
            "truncated": truncated,
            "extraction_mode": extraction_mode,
        }
        self._audit(
            "extract_document_text",
            "success",
            source_id=str(resolved["source_id"]),
            target_path_or_item_id=target_identifier,
            item_type="file",
            duration_ms=_duration_ms(started),
            bytes_read=path.stat().st_size,
        )
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        return result

    def summarize_item(
        self,
        item_ref: Mapping[str, Any] | str,
        *,
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        resolved = self._resolve_item_input(item_ref)
        path = self._path_from_item_ref(resolved)
        if path.suffix.lower() in DEFAULT_DOCUMENT_EXTENSIONS:
            extracted = self.extract_document_text(resolved, max_chars=max_chars)
            text = extracted["content"]
        else:
            extracted = self.read_text_item(resolved, max_chars=max_chars)
            text = extracted["content"]
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        title_guess = lines[0] if lines else path.name
        outline = [line for line in lines if line.startswith(("#", "##", "###"))][:10]
        excerpt = "\n".join(lines[:10])[:max_chars]
        return {
            "title_guess": title_guess,
            "outline": outline,
            "excerpt": excerpt,
        }

    def open_text_edit_session(
        self,
        item_ref: Mapping[str, Any] | str,
        *,
        create_if_missing: bool = False,
        remote_mode: str | None = None,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        resolved = self._resolve_item_input(
            item_ref,
            allow_missing=create_if_missing,
        )
        source = self._require_source(str(resolved["source_id"]))
        if source.read_only:
            self._raise_error("SOURCE_READ_ONLY", f"Source '{source.source_id}' is read-only.")
        if bool(resolved.get("read_only")):
            self._raise_error("SOURCE_READ_ONLY", "Target item is read-only.")
        if resolved["item_type"] != "file":
            self._raise_error("ITEM_TYPE_MISMATCH", "Text edit sessions require a file target.")
        self._ensure_text_edit_allowed(resolved, create_if_missing=create_if_missing)
        temp_dir = self._resolve_temp_session_dir(source)
        temp_dir.mkdir(parents=True, exist_ok=False)
        if self._source_is_remote(source):
            target_name = PurePosixPath(str(resolved.get("remote_path") or "")).name or "remote.txt"
            temp_path = temp_dir / target_name
            if resolved.get("remote_item_id") is not None:
                remote_item = self._graph_item_from_ref(resolved)
                self._remote_client(source).download_to(remote_item, temp_path)
                raw_bytes = temp_path.read_bytes()
                detected_encoding = _decode_text_bytes(raw_bytes, encoding=None)[1]
                base_hash = _sha256_file(temp_path)
                base_size = len(raw_bytes)
                base_modified_at = _optional_iso(str(resolved.get("modified_at")))
                base_etag = (
                    str(resolved["etag"]) if isinstance(resolved.get("etag"), str) else None
                )
                initial_exists = True
            else:
                temp_path.write_text("", encoding="utf-8")
                detected_encoding = "utf-8"
                base_hash = None
                base_size = None
                base_modified_at = None
                base_etag = None
                initial_exists = False
        else:
            target_path = self._path_from_item_ref(resolved)
            temp_path = temp_dir / target_path.name
            if target_path.exists():
                shutil.copy2(target_path, temp_path)
                raw_bytes = target_path.read_bytes()
                detected_encoding = _decode_text_bytes(raw_bytes, encoding=None)[1]
                base_hash = _sha256_file(target_path)
                base_size = target_path.stat().st_size
                base_modified_at = _mtime_iso(target_path)
                base_etag = None
                initial_exists = True
            else:
                temp_path.write_text("", encoding="utf-8")
                detected_encoding = "utf-8"
                base_hash = None
                base_size = None
                base_modified_at = None
                base_etag = None
                initial_exists = False
        now = datetime.now(UTC)
        session_id = uuid4().hex
        session = FileEditSession(
            session_id=session_id,
            source_id=source.source_id,
            target_item_ref=resolved,
            target_mode="remote_roundtrip" if self._source_is_remote(source) else "local",
            base_hash=base_hash,
            base_etag=base_etag,
            base_size=base_size,
            base_modified_at=base_modified_at,
            temp_local_copy=temp_path,
            opened_at=now,
            expires_at=now + timedelta(seconds=self._config.limits.hard_timeout_sec),
            actor=self._actor(),
            audit_ref=self._audit_file.relative_to(self._workspace_root).as_posix(),
            initial_exists=initial_exists,
            item_type="file",
            metadata={
                "encoding": detected_encoding,
                "newline": _detect_newline_style(temp_path.read_text(encoding=detected_encoding)),
                "text_session": True,
            },
        )
        self._sessions[session_id] = session
        result = {
            "session_id": session_id,
            "base_hash": base_hash,
            "base_etag": base_etag,
            "expires_at": session.expires_at.isoformat(),
        }
        self._audit(
            "open_text_edit_session",
            "success",
            source_id=source.source_id,
            target_path_or_item_id=self._item_location(resolved),
            item_type="file",
            duration_ms=_duration_ms(started),
        )
        return result

    def stage_replace_text(
        self,
        session_id: str,
        content: str,
        *,
        encoding: str | None = None,
        newline_mode: str = "preserve",
        expected_base_hash: str | None = None,
    ) -> dict[str, Any]:
        session = self._require_text_session(session_id)
        if expected_base_hash is not None and expected_base_hash != session.base_hash:
            self._raise_error(
                "HASH_MISMATCH", "expected_base_hash does not match the session base_hash."
            )
        before_text, effective_encoding = self._read_session_text(session, encoding=encoding)
        after_text = _apply_newline_mode(
            content,
            newline_mode=newline_mode,
            existing_newline=str(session.metadata.get("newline", "\n")),
        )
        self._ensure_inline_write_size(after_text)
        self._write_session_text(session, after_text, encoding=effective_encoding)
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="replace_text",
            target_path=self._item_location(session.target_item_ref),
            warnings=[],
            risk_flags=self._text_risk_flags(session, before_text, after_text),
            metadata={"encoding": effective_encoding, "newline_mode": newline_mode},
            before_text=before_text,
            after_text=after_text,
        )
        self._record_operation(session, operation)
        return {"operation_id": operation.operation_id}

    def stage_patch_text(
        self,
        session_id: str,
        patch_type: str,
        operations: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        session = self._require_text_session(session_id)
        before_text, encoding = self._read_session_text(session)
        after_text, warnings = _apply_text_patch(
            before_text,
            patch_type=patch_type,
            operations=operations,
            max_regex_replace_count=self._config.policies.max_regex_replace_count,
        )
        self._ensure_inline_write_size(after_text)
        self._write_session_text(session, after_text, encoding=encoding)
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="patch_text",
            target_path=self._item_location(session.target_item_ref),
            warnings=warnings,
            risk_flags=self._text_risk_flags(session, before_text, after_text),
            metadata={"patch_type": patch_type, "operation_count": len(list(operations))},
            before_text=before_text,
            after_text=after_text,
        )
        self._record_operation(session, operation)
        return {"operation_id": operation.operation_id}

    def stage_insert_text(
        self,
        session_id: str,
        *,
        position: str,
        content: str,
        byte_offset: int | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        session = self._require_text_session(session_id)
        before_text, encoding = self._read_session_text(session)
        after_text = _insert_text(
            before_text,
            content=content,
            position=position,
            byte_offset=byte_offset,
            line_number=line_number,
            encoding=encoding,
        )
        self._ensure_inline_write_size(after_text)
        self._write_session_text(session, after_text, encoding=encoding)
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="insert_text",
            target_path=self._item_location(session.target_item_ref),
            risk_flags=self._text_risk_flags(session, before_text, after_text),
            metadata={
                "position": position,
                "byte_offset": byte_offset,
                "line_number": line_number,
            },
            before_text=before_text,
            after_text=after_text,
        )
        self._record_operation(session, operation)
        return {"operation_id": operation.operation_id}

    def stage_append_text(self, session_id: str, content: str) -> dict[str, Any]:
        return self.stage_insert_text(session_id, position="end", content=content)

    def stage_create_text_file(
        self,
        parent_folder_ref: Mapping[str, Any] | str,
        file_name: str,
        *,
        encoding: str = "utf-8",
        content: str = "",
        if_exists: str = "fail",
    ) -> dict[str, Any]:
        parent_item = self._resolve_item_input(parent_folder_ref)
        if parent_item["item_type"] != "folder":
            self._raise_error("ITEM_TYPE_MISMATCH", "parent_folder_ref must resolve to a folder.")
        target_name = file_name.strip()
        if not target_name:
            self._raise_error("VALIDATION_FAILED", "file_name must be non-empty.")
        source = self._require_source(str(parent_item["source_id"]))
        if self._source_is_remote(source):
            target_remote_path = self._join_remote_path(
                str(parent_item.get("remote_path") or "/"),
                target_name,
            )
            item_ref = self._prepare_remote_create_target(
                source=source,
                remote_path=target_remote_path,
                if_exists=if_exists,
            )
        else:
            parent_path = self._path_from_item_ref(parent_item)
            target_path = parent_path / target_name
            if target_path.exists():
                if if_exists == "fail":
                    self._raise_error(
                        "OVERWRITE_NOT_ALLOWED",
                        f"File '{target_name}' already exists.",
                    )
                if if_exists == "overwrite_if_empty":
                    if target_path.stat().st_size != 0:
                        self._raise_error(
                            "OVERWRITE_NOT_ALLOWED",
                            f"File '{target_name}' is not empty.",
                        )
                elif if_exists == "create_numbered_copy":
                    target_path = _next_numbered_copy(target_path)
                else:
                    self._raise_error(
                        "VALIDATION_FAILED", f"Unsupported if_exists policy '{if_exists}'."
                    )
            item_ref = self._build_item_ref(source, target_path, allow_missing=True)
        opened = self.open_text_edit_session(item_ref, create_if_missing=True)
        session_id = str(opened["session_id"])
        self.stage_replace_text(
            session_id,
            content,
            encoding=encoding,
            newline_mode="preserve",
        )
        session = self._sessions[session_id]
        if session.staged_operations:
            session.staged_operations[-1].operation_type = "create_text_file"
        return {
            "session_id": session_id,
            "operation_id": session.staged_operations[-1].operation_id,
            "target_item_ref": item_ref,
        }

    def stage_rename_item(
        self,
        *,
        new_name: str,
        session_id: str | None = None,
        item_ref: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        target_name = new_name.strip()
        if not target_name:
            self._raise_error("VALIDATION_FAILED", "new_name must be non-empty.")
        session = self._ensure_structural_session(session_id=session_id, item_ref=item_ref)
        current_path = self._path_from_item_ref(session.target_item_ref)
        destination = current_path.with_name(target_name)
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="rename_item",
            target_path=str(session.target_item_ref.get("local_path")),
            risk_flags=["overwrites_existing_file"] if destination.exists() else [],
            metadata={
                "new_name": target_name,
                "destination_path": self._local_relpath(
                    destination, self._require_source(session.source_id)
                ),
            },
        )
        self._record_operation(session, operation)
        return {"session_id": session.session_id, "operation_id": operation.operation_id}

    def stage_move_item(
        self,
        *,
        destination_folder_ref: Mapping[str, Any] | str,
        conflict_policy: str = "fail",
        session_id: str | None = None,
        item_ref: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        session = self._ensure_structural_session(session_id=session_id, item_ref=item_ref)
        destination_folder = self._resolve_item_input(destination_folder_ref)
        if destination_folder["item_type"] != "folder":
            self._raise_error(
                "ITEM_TYPE_MISMATCH", "destination_folder_ref must resolve to a folder."
            )
        source = self._require_source(session.source_id)
        current_path = self._path_from_item_ref(session.target_item_ref)
        destination_path = self._path_from_item_ref(destination_folder) / current_path.name
        risk_flags = []
        if destination_path.exists():
            risk_flags.append("overwrites_existing_file")
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="move_item",
            target_path=str(session.target_item_ref.get("local_path")),
            risk_flags=risk_flags,
            metadata={
                "destination_folder": destination_folder.get("local_path"),
                "destination_path": self._local_relpath(destination_path, source),
                "conflict_policy": conflict_policy,
            },
        )
        self._record_operation(session, operation)
        return {"session_id": session.session_id, "operation_id": operation.operation_id}

    def stage_copy_item(
        self,
        *,
        destination_folder_ref: Mapping[str, Any] | str,
        new_name: str | None = None,
        overwrite: bool = False,
        session_id: str | None = None,
        item_ref: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        session = self._ensure_structural_session(session_id=session_id, item_ref=item_ref)
        destination_folder = self._resolve_item_input(destination_folder_ref)
        if destination_folder["item_type"] != "folder":
            self._raise_error(
                "ITEM_TYPE_MISMATCH", "destination_folder_ref must resolve to a folder."
            )
        source = self._require_source(session.source_id)
        current_path = self._path_from_item_ref(session.target_item_ref)
        destination_name = (
            new_name.strip()
            if isinstance(new_name, str) and new_name.strip()
            else current_path.name
        )
        destination_path = self._path_from_item_ref(destination_folder) / destination_name
        risk_flags = []
        if destination_path.exists():
            risk_flags.append("overwrites_existing_file")
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="copy_item",
            target_path=str(session.target_item_ref.get("local_path")),
            risk_flags=risk_flags,
            metadata={
                "destination_folder": destination_folder.get("local_path"),
                "destination_path": self._local_relpath(destination_path, source),
                "overwrite": overwrite,
            },
        )
        self._record_operation(session, operation)
        return {"session_id": session.session_id, "operation_id": operation.operation_id}

    def stage_create_folder(
        self,
        parent_folder_ref: Mapping[str, Any] | str,
        folder_name: str,
    ) -> dict[str, Any]:
        parent_item = self._resolve_item_input(parent_folder_ref)
        if parent_item["item_type"] != "folder":
            self._raise_error("ITEM_TYPE_MISMATCH", "parent_folder_ref must resolve to a folder.")
        target_name = folder_name.strip()
        if not target_name:
            self._raise_error("VALIDATION_FAILED", "folder_name must be non-empty.")
        source = self._require_source(str(parent_item["source_id"]))
        target_path = self._path_from_item_ref(parent_item) / target_name
        item_ref = self._build_item_ref(
            source, target_path, allow_missing=True, force_item_type="folder"
        )
        session = self._create_structural_session(item_ref)
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="create_folder",
            target_path=str(item_ref.get("local_path")),
            risk_flags=["overwrites_existing_file"] if target_path.exists() else [],
            metadata={},
        )
        self._record_operation(session, operation)
        return {
            "session_id": session.session_id,
            "operation_id": operation.operation_id,
            "target_item_ref": item_ref,
        }

    def stage_delete_item(
        self,
        *,
        deletion_mode: str = "soft_delete_preferred",
        session_id: str | None = None,
        item_ref: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        if not self._config.policies.delete_enabled:
            self._raise_error("DELETE_DISABLED", "Delete is disabled by policy.")
        session = self._ensure_structural_session(session_id=session_id, item_ref=item_ref)
        operation = FileStagedOperation(
            operation_id=uuid4().hex,
            operation_type="delete_item",
            target_path=str(session.target_item_ref.get("local_path")),
            risk_flags=["binary_no_inline_diff"],
            metadata={"deletion_mode": deletion_mode},
        )
        self._record_operation(session, operation)
        return {"session_id": session.session_id, "operation_id": operation.operation_id}

    def preview_file_edit_session(self, session_id: str) -> dict[str, Any]:
        started = time_module.perf_counter()
        session = self._require_session(session_id)
        text_diffs: list[dict[str, Any]] = []
        structural_preview: list[dict[str, Any]] = []
        risk_flags: list[str] = []
        for operation in session.staged_operations:
            risk_flags.extend(operation.risk_flags)
            if operation.before_text is not None and operation.after_text is not None:
                text_diffs.append(
                    {
                        "operation_id": operation.operation_id,
                        "operation_type": operation.operation_type,
                        **_diff_summary(operation.before_text, operation.after_text),
                    }
                )
            else:
                structural_preview.append(
                    {
                        "operation_id": operation.operation_id,
                        "operation_type": operation.operation_type,
                        "target_path": operation.target_path,
                        "metadata": dict(operation.metadata),
                    }
                )
        preview = {
            "metadata_preview": {
                "session_id": session.session_id,
                "target_path": self._item_location(session.target_item_ref),
                "operation_count": len(session.staged_operations),
            },
            "text_diff_preview": text_diffs,
            "structural_preview": structural_preview,
            "risk_preview": {
                "risk_flags": _unique_list(risk_flags),
            },
        }
        session.preview_summary = preview
        session.state = "PREVIEWED"
        self._audit(
            "preview_file_edit_session",
            "success",
            source_id=session.source_id,
            target_path_or_item_id=self._item_location(session.target_item_ref),
            item_type=session.item_type,
            duration_ms=_duration_ms(started),
            risk_flags=_unique_list(risk_flags),
            session_id=session.session_id,
        )
        return {"preview": preview}

    def validate_file_edit_session(self, session_id: str) -> dict[str, Any]:  # noqa: C901
        started = time_module.perf_counter()
        session = self._require_session(session_id)
        source = self._require_source(session.source_id)
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if self._source_is_remote(source):
            self._validate_remote_session(
                session=session,
                errors=errors,
                warnings=warnings,
            )
        else:
            target_path = self._path_from_item_ref(session.target_item_ref)
            if session.base_hash is not None and target_path.exists():
                current_hash = _sha256_file(target_path)
                if current_hash != session.base_hash:
                    errors.append(
                        {
                            "code": "CONFLICT_DETECTED",
                            "message": "Target changed since the session was opened.",
                        }
                    )
            if session.base_modified_at is not None and target_path.exists():
                current_modified_at = _mtime_iso(target_path)
                if current_modified_at != session.base_modified_at:
                    errors.append(
                        {
                            "code": "CONFLICT_DETECTED",
                            "message": "Target modified_at changed since the session was opened.",
                        }
                    )
        if (
            session.temp_local_copy is not None
            and session.temp_local_copy.exists()
            and session.temp_local_copy.stat().st_size
            > self._config.limits.max_inline_write_chars * 4
        ):
            errors.append(
                {
                    "code": "CONTENT_TOO_LARGE",
                    "message": "Staged content exceeds the inline write limit.",
                }
            )
        if source.read_only or bool(session.target_item_ref.get("read_only")):
            errors.append(
                {
                    "code": "SOURCE_READ_ONLY",
                    "message": f"Source '{source.source_id}' is read-only.",
                }
            )
        for operation in session.staged_operations:
            if (
                operation.operation_type == "delete_item"
                and not self._config.policies.delete_enabled
            ):
                errors.append(
                    {"code": "DELETE_DISABLED", "message": "Delete is disabled by policy."}
                )
            if operation.operation_type in {
                "rename_item",
                "move_item",
                "copy_item",
                "create_folder",
            }:
                destination_path = (
                    operation.metadata.get("destination_path") or operation.target_path
                )
                if isinstance(destination_path, str) and ".." in destination_path:
                    errors.append(
                        {
                            "code": "VALIDATION_FAILED",
                            "message": "cross boundary move attempt detected.",
                        }
                    )
        self._validate_session_extensions(session=session, errors=errors)
        valid = not errors
        result = {"valid": valid, "errors": errors, "warnings": warnings}
        session.validation_summary = result
        if valid:
            session.state = "VALIDATED"
        self._audit(
            "validate_file_edit_session",
            "success" if valid else "failed",
            source_id=session.source_id,
            target_path_or_item_id=self._item_location(session.target_item_ref),
            item_type=session.item_type,
            duration_ms=_duration_ms(started),
            risk_flags=[error["code"] for error in errors if "code" in error],
            session_id=session.session_id,
        )
        return result

    def commit_file_edit_session(  # noqa: C901
        self,
        session_id: str,
        *,
        commit_message: str | None = None,
        require_previewed: bool = True,
        require_validated: bool | None = None,
    ) -> dict[str, Any]:
        started = time_module.perf_counter()
        session = self._require_session(session_id)
        if not session.staged_operations:
            self._raise_error("VALIDATION_FAILED", "No staged operations were found.")
        if require_previewed and session.preview_summary is None:
            self._raise_error(
                "PREVIEW_REQUIRED",
                "preview_file_edit_session must be called before commit.",
            )
        source = self._require_source(session.source_id)
        effective_require_validation = (
            self._config.policies.commit_requires_validation
            if require_validated is None
            else require_validated
        )
        if effective_require_validation and (
            session.validation_summary is None or not bool(session.validation_summary.get("valid"))
        ):
            self._raise_error(
                "VALIDATION_FAILED",
                "validate_file_edit_session must succeed before commit.",
            )
        if self._source_is_remote(source):
            result = self._commit_remote_session(
                session=session,
                source=source,
                commit_message=commit_message,
                started=started,
            )
            return result
        target_path = self._path_from_item_ref(session.target_item_ref)
        if session.base_hash is not None and target_path.exists():
            current_hash = _sha256_file(target_path)
            if current_hash != session.base_hash:
                self._raise_error(
                    "CONFLICT_DETECTED",
                    "Target changed since the session was opened.",
                    detail={"current_hash": current_hash, "base_hash": session.base_hash},
                )
            if (
                session.base_modified_at is not None
                and _mtime_iso(target_path) != session.base_modified_at
            ):
                self._raise_error(
                    "CONFLICT_DETECTED",
                    "Target modified_at changed since the session was opened.",
                )

        backup_ref = None
        if self._config.policies.auto_backup and target_path.exists() and target_path.is_file():
            backup_ref = self._create_backup(
                source=source, target_path=target_path, session=session
            )

        current_path = target_path
        changed_targets: list[str] = []
        if _has_text_operations(session) and session.temp_local_copy is not None:
            current_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(session.temp_local_copy, current_path)
            changed_targets.append(self._local_relpath(current_path, source))

        for operation in session.staged_operations:
            if operation.operation_type in {
                "replace_text",
                "patch_text",
                "insert_text",
                "append_text",
                "create_text_file",
            }:
                continue
            current_path = self._apply_structural_operation(
                source=source,
                current_path=current_path,
                session=session,
                operation=operation,
                changed_targets=changed_targets,
            )

        final_item_ref = self._build_item_ref(
            source,
            current_path,
            allow_missing=not current_path.exists(),
            force_item_type=session.item_type,
        )
        result = {
            "commit_id": uuid4().hex,
            "final_item_ref": final_item_ref,
            "backup_ref": backup_ref,
            "changed_targets": _unique_list(changed_targets),
            "commit_message": commit_message,
        }
        session.state = "COMMITTED"
        session.commit_result = result
        session.backup_ref = backup_ref
        self._cleanup_session(session)
        self._audit(
            "commit_file_edit_session",
            "success",
            source_id=session.source_id,
            target_path_or_item_id=self._item_location(final_item_ref),
            item_type=session.item_type,
            duration_ms=_duration_ms(started),
            risk_flags=[
                risk for operation in session.staged_operations for risk in operation.risk_flags
            ],
            session_id=session.session_id,
            backup_ref=backup_ref["backup_ref"] if isinstance(backup_ref, dict) else None,
            bytes_written=current_path.stat().st_size
            if current_path.exists() and current_path.is_file()
            else None,
        )
        return result

    def cancel_file_edit_session(self, session_id: str) -> dict[str, Any]:
        started = time_module.perf_counter()
        session = self._require_session(session_id)
        session.state = "CANCELED"
        self._cleanup_session(session)
        self._audit(
            "cancel_file_edit_session",
            "success",
            source_id=session.source_id,
            target_path_or_item_id=self._item_location(session.target_item_ref),
            item_type=session.item_type,
            duration_ms=_duration_ms(started),
            session_id=session.session_id,
        )
        return {"canceled": True}

    def list_backups(
        self,
        source_id: str,
        *,
        target: Mapping[str, Any] | str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        source = self._require_local_source(source_id)
        if limit <= 0:
            self._raise_error("VALIDATION_FAILED", "limit must be greater than zero.")
        target_path = None
        if target is not None:
            target_ref = self._resolve_item_input(
                target, default_source_id=source_id, allow_missing=True
            )
            target_path = str(target_ref.get("local_path"))
        backups: list[dict[str, Any]] = []
        for metadata_path in sorted(
            source.backup_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            if target_path is not None and payload.get("original_path") != target_path:
                continue
            backups.append(payload)
            if len(backups) >= limit:
                break
        return {"backups": backups}

    def restore_backup(
        self,
        backup_ref: Mapping[str, Any] | str,
        *,
        target_override: str | None = None,
    ) -> dict[str, Any]:
        metadata = self._resolve_backup_metadata(backup_ref)
        source = self._require_local_source(str(metadata["source_id"]))
        backup_path = self._resolve_path_inside_root(
            str(metadata["backup_path"]), source.backup_dir
        )
        target_path = (
            self._resolve_path_inside_root(target_override, self._source_root(source))
            if target_override is not None
            else self._resolve_path_inside_root(
                str(metadata["original_path"]), self._source_root(source)
            )
        )
        shutil.copy2(backup_path, target_path)
        return {
            "restore_result": {
                "restored": True,
                "backup_ref": metadata["backup_ref"],
                "target": self._local_relpath(target_path, source),
            }
        }

    # Compatibility API used by the current planner and tests.

    def list_entries(self, relative_path: str = ".") -> list[dict[str, Any]]:
        target_dir = self._resolve_path_inside_root(relative_path, self._workspace_root)
        if not target_dir.exists():
            raise FileNotFoundError(f"Directory '{relative_path}' does not exist.")
        if not target_dir.is_dir():
            raise NotADirectoryError(f"Path '{relative_path}' is not a directory.")
        entries: list[dict[str, Any]] = []
        for child in sorted(
            target_dir.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        ):
            if target_dir == self._workspace_root and child.name in _INTERNAL_ENTRY_NAMES:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": child.relative_to(self._workspace_root).as_posix(),
                    "is_dir": child.is_dir(),
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return entries

    def find_entries(
        self,
        pattern: str,
        path: str = ".",
        *,
        case_sensitive: bool = False,
        regex: bool = False,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> dict[str, Any]:
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero.")
        target_dir = self._resolve_path_inside_root(path, self._workspace_root)
        if not target_dir.exists():
            raise FileNotFoundError(f"Directory '{path}' does not exist.")
        if not target_dir.is_dir():
            raise NotADirectoryError(f"Path '{path}' is not a directory.")
        matches: list[dict[str, Any]] = []
        truncated = False
        for candidate in sorted(target_dir.rglob("*")):
            if candidate.is_dir() and not include_dirs:
                continue
            relative_candidate = candidate.relative_to(self._workspace_root).as_posix()
            if not _matches_text(
                relative_candidate,
                pattern,
                case_sensitive=case_sensitive,
                regex=regex,
            ):
                continue
            matches.append(
                {
                    "name": candidate.name,
                    "path": relative_candidate,
                    "is_dir": candidate.is_dir(),
                }
            )
            if len(matches) >= max_results:
                truncated = True
                break
        return {
            "path": target_dir.relative_to(self._workspace_root).as_posix(),
            "pattern": pattern,
            "matches": matches,
            "truncated": truncated,
        }

    def read_text(self, relative_path: str, encoding: str = "utf-8") -> str:
        target = self._resolve_path_inside_root(relative_path, self._workspace_root)
        if not target.exists():
            raise FileNotFoundError(f"File '{relative_path}' does not exist.")
        if not target.is_file():
            raise IsADirectoryError(f"Path '{relative_path}' is not a file.")
        raw_bytes = target.read_bytes()
        if len(raw_bytes) > self._max_read_bytes:
            raise ValueError(
                f"File '{relative_path}' is too large ({len(raw_bytes)} bytes). "
                f"Limit is {self._max_read_bytes} bytes."
            )
        return _decode_text_bytes(raw_bytes, encoding=encoding)[0]

    def grep_text(  # noqa: C901
        self,
        pattern: str,
        path: str = ".",
        *,
        case_sensitive: bool = False,
        regex: bool = False,
        file_glob: str | None = None,
        max_results: int = 200,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero.")
        target_dir = self._resolve_path_inside_root(path, self._workspace_root)
        if not target_dir.exists():
            raise FileNotFoundError(f"Directory '{path}' does not exist.")
        if not target_dir.is_dir():
            raise NotADirectoryError(f"Path '{path}' is not a directory.")
        matches: list[dict[str, Any]] = []
        truncated = False
        iterator = target_dir.rglob(file_glob) if file_glob else target_dir.rglob("*")
        for candidate in sorted(iterator):
            if not candidate.is_file():
                continue
            if candidate.stat().st_size > self._max_read_bytes:
                continue
            try:
                content = _decode_text_bytes(candidate.read_bytes(), encoding=encoding)[0]
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if not _matches_text(line, pattern, case_sensitive=case_sensitive, regex=regex):
                    continue
                matches.append(
                    {
                        "path": candidate.relative_to(self._workspace_root).as_posix(),
                        "line_number": line_number,
                        "line": line,
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break
            if truncated:
                break
        return {
            "path": target_dir.relative_to(self._workspace_root).as_posix(),
            "pattern": pattern,
            "matches": matches,
            "truncated": truncated,
        }

    def write_text(
        self,
        relative_path: str,
        content: str,
        overwrite: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        target = self._resolve_path_inside_root(relative_path, self._workspace_root)
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"File '{relative_path}' already exists. Set overwrite=True to replace it."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite and target.exists() and target.is_file():
            self._create_compat_backup(target)
        target.write_text(content, encoding=encoding)
        return {
            "path": target.relative_to(self._workspace_root).as_posix(),
            "bytes": target.stat().st_size,
        }

    def copy_file(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = self._resolve_path_inside_root(source_path, self._workspace_root)
        destination = self._resolve_path_inside_root(destination_path, self._workspace_root)
        if source == destination:
            raise ValueError("source_path and destination_path must be different.")
        if not source.exists():
            raise FileNotFoundError(f"File '{source_path}' does not exist.")
        if not source.is_file():
            raise IsADirectoryError(f"Path '{source_path}' is not a file.")
        if destination.exists() and not overwrite:
            raise FileExistsError(
                f"File '{destination_path}' already exists. Set overwrite=True to replace it."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return {
            "source": source.relative_to(self._workspace_root).as_posix(),
            "destination": destination.relative_to(self._workspace_root).as_posix(),
            "bytes": destination.stat().st_size,
        }

    def _normalize_sources(
        self,
        sources: Sequence[FileSourceProfile],
    ) -> dict[str, FileSourceProfile]:
        if not sources:
            fallback = FileServerConfig.default(self._workspace_root)
            sources = fallback.sources
        normalized: dict[str, FileSourceProfile] = {}
        for source in sources:
            if source.source_id in normalized:
                self._raise_error("VALIDATION_FAILED", f"Duplicate source_id '{source.source_id}'.")
            if source.source_type == "local_workspace":
                root = self._resolve_inside_workspace(source.workspace_root or self._workspace_root)
                source.workspace_root = root
                source.temp_root = self._resolve_inside_workspace(
                    source.temp_root or (root / ".file_mcp_tmp")
                )
                source.backup_dir = self._resolve_inside_workspace(
                    source.backup_dir or (root / ".file_mcp_backups")
                )
                source.temp_root.mkdir(parents=True, exist_ok=True)
                source.backup_dir.mkdir(parents=True, exist_ok=True)
            else:
                source.temp_root = self._resolve_inside_workspace(
                    source.temp_root or (self._workspace_root / ".file_mcp_tmp" / source.source_id)
                )
                source.backup_dir = self._resolve_inside_workspace(
                    source.backup_dir
                    or (self._workspace_root / ".file_mcp_backups" / source.source_id)
                )
                source.temp_root.mkdir(parents=True, exist_ok=True)
                source.backup_dir.mkdir(parents=True, exist_ok=True)
            normalized[source.source_id] = source
        return normalized

    @staticmethod
    def _normalize_aliases(aliases: Sequence[ManifestAlias]) -> dict[str, ManifestAlias]:
        return {alias.alias: alias for alias in aliases}

    @staticmethod
    def _normalize_auth_profiles(
        auth_profiles: Sequence[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for item in auth_profiles:
            profile = dict(item)
            profile_id = profile.get("auth_profile_id") or profile.get("id")
            if not isinstance(profile_id, str) or not profile_id.strip():
                continue
            normalized[profile_id.strip()] = profile
        return normalized

    @staticmethod
    def _source_is_remote(source: FileSourceProfile) -> bool:
        return source.source_type != "local_workspace"

    def _ensure_remote_enabled(self, source: FileSourceProfile) -> None:
        if not self._source_is_remote(source):
            return
        if self._remote_enabled is False:
            self._raise_error(
                "SOURCE_DISABLED",
                f"Remote source '{source.source_id}' is disabled by FILE_MCP_REMOTE_ENABLED.",
            )
        if source.auth_profile is None:
            self._raise_error(
                "AUTH_REQUIRED",
                f"Remote source '{source.source_id}' requires auth_profile.",
            )

    def _remote_client(self, source: FileSourceProfile) -> GraphFileClient:
        self._ensure_remote_enabled(source)
        client = self._remote_clients.get(source.source_id)
        if client is not None:
            return client
        auth_profile_name = source.auth_profile or ""
        auth_profile = self._auth_profiles.get(auth_profile_name)
        if auth_profile is None:
            self._raise_error(
                "AUTH_REQUIRED",
                f"Auth profile '{auth_profile_name}' was not found.",
            )
        client = GraphFileClient(
            source,
            auth_profile,
            transport=self._graph_transport,
            graph_base_url=self._graph_base_url,
        )
        self._remote_clients[source.source_id] = client
        return client

    def _remote_search_base_paths(
        self,
        source: FileSourceProfile,
        *,
        parent: Mapping[str, Any] | str | None,
    ) -> list[str]:
        if parent is not None:
            parent_ref = self._resolve_item_input(parent, default_source_id=source.source_id)
            if parent_ref["item_type"] != "folder":
                self._raise_error("ITEM_TYPE_MISMATCH", "parent must resolve to a folder.")
            return [str(parent_ref.get("remote_path") or "/")]
        if source.search_mode == "manifest_only":
            alias_paths = [
                alias.base_folder_path
                for alias in self._aliases.values()
                if alias.source_id == source.source_id
            ]
            return alias_paths
        if source.search_mode in {"path_prefix_walk", "graph_search"}:
            return ["/"]
        if source.search_mode == "direct_path_only":
            return []
        return ["/"]

    def _resolve_remote_item_input(
        self,
        source: FileSourceProfile,
        item_ref: Mapping[str, Any],
        *,
        allow_missing: bool,
    ) -> dict[str, Any]:
        remote_path = item_ref.get("remote_path") or item_ref.get("path")
        item_id = item_ref.get("remote_item_id")
        drive_id = item_ref.get("drive_id")
        force_item_type = (
            item_ref.get("item_type") if isinstance(item_ref.get("item_type"), str) else None
        )
        remote_item = self._remote_client(source).resolve_item(
            remote_path=str(remote_path) if isinstance(remote_path, str) else None,
            item_id=str(item_id) if isinstance(item_id, str) else None,
            drive_id=str(drive_id) if isinstance(drive_id, str) else None,
            allow_missing=allow_missing,
            force_item_type=force_item_type,  # type: ignore[arg-type]
        )
        resolved = remote_item.to_item_ref()
        self._merge_item_ref_metadata(resolved, item_ref)
        return resolved

    @staticmethod
    def _merge_item_ref_metadata(
        resolved: dict[str, Any],
        original: Mapping[str, Any],
    ) -> None:
        for key in ("manifest_alias", "read_only", "web_url"):
            if key in original:
                resolved[key] = original.get(key)

    @staticmethod
    def _item_location(item_ref: Mapping[str, Any]) -> str:
        location = item_ref.get("local_path") or item_ref.get("remote_path") or item_ref.get("path")
        return str(location or "")

    def _item_extension(self, item_ref: Mapping[str, Any]) -> str:
        location = self._item_location(item_ref)
        if not location:
            return ""
        if self._source_is_remote(self._require_source(str(item_ref["source_id"]))):
            return PurePosixPath(location).suffix.lower()
        return Path(location).suffix.lower()

    def _graph_item_from_ref(self, item_ref: Mapping[str, Any]) -> GraphDriveItem:
        source = self._require_source(str(item_ref["source_id"]))
        if not self._source_is_remote(source):
            self._raise_error("ITEM_TYPE_MISMATCH", "Remote item_ref is required.")
        remote_path = item_ref.get("remote_path")
        if not isinstance(remote_path, str) or not remote_path.strip():
            self._raise_error("ITEM_NOT_FOUND", "item_ref.remote_path is required.")
        return GraphDriveItem(
            source_id=source.source_id,
            item_id=str(item_ref["remote_item_id"])
            if isinstance(item_ref.get("remote_item_id"), str)
            else None,
            item_type="folder" if item_ref.get("item_type") == "folder" else "file",
            name=PurePosixPath(remote_path).name,
            remote_path=remote_path,
            drive_id=str(item_ref["drive_id"])
            if isinstance(item_ref.get("drive_id"), str)
            else self._remote_client(source).drive_id(),
            site_id=str(item_ref["site_id"]) if isinstance(item_ref.get("site_id"), str) else None,
            etag=str(item_ref["etag"]) if isinstance(item_ref.get("etag"), str) else None,
            size=int(item_ref["size"])
            if isinstance(item_ref.get("size"), int) and not isinstance(item_ref.get("size"), bool)
            else None,
            modified_at=(
                str(item_ref["modified_at"])
                if isinstance(item_ref.get("modified_at"), str)
                else None
            ),
            media_type=(
                str(item_ref["media_type"]) if isinstance(item_ref.get("media_type"), str) else None
            ),
            web_url=str(item_ref["web_url"]) if isinstance(item_ref.get("web_url"), str) else None,
        )

    def _join_remote_path(self, parent_path: str, child_name: str) -> str:
        normalized_parent = PurePosixPath("/" + parent_path.lstrip("/")).as_posix()
        if normalized_parent == ".":
            normalized_parent = "/"
        if normalized_parent == "/":
            return f"/{child_name}"
        return f"{normalized_parent.rstrip('/')}/{child_name}"

    def _prepare_remote_create_target(
        self,
        *,
        source: FileSourceProfile,
        remote_path: str,
        if_exists: str,
    ) -> dict[str, Any]:
        client = self._remote_client(source)
        candidate = remote_path
        while True:
            existing = client.resolve_item(
                remote_path=candidate,
                allow_missing=True,
                force_item_type="file",
            )
            if existing.item_id is None:
                return existing.to_item_ref()
            if if_exists == "fail":
                self._raise_error("OVERWRITE_NOT_ALLOWED", f"File '{candidate}' already exists.")
            if if_exists == "overwrite_if_empty":
                if (existing.size or 0) != 0:
                    self._raise_error(
                        "OVERWRITE_NOT_ALLOWED",
                        f"File '{candidate}' is not empty.",
                    )
                return existing.to_item_ref()
            if if_exists == "create_numbered_copy":
                candidate = _next_numbered_remote_path(candidate)
                continue
            self._raise_error("VALIDATION_FAILED", f"Unsupported if_exists policy '{if_exists}'.")

    def _resolve_temp_session_dir(self, source: FileSourceProfile) -> Path:
        return self._resolve_path_inside_root(uuid4().hex, source.temp_root or self._workspace_root)

    def _download_remote_item_to_temp(
        self,
        source: FileSourceProfile,
        item_ref: Mapping[str, Any],
    ) -> tuple[Path, Path]:
        remote_item = self._graph_item_from_ref(item_ref)
        temp_dir = self._resolve_temp_session_dir(source)
        temp_dir.mkdir(parents=True, exist_ok=False)
        temp_name = PurePosixPath(remote_item.remote_path).name or "remote.bin"
        temp_path = temp_dir / temp_name
        self._remote_client(source).download_to(remote_item, temp_path)
        return temp_path, temp_dir

    @staticmethod
    def _matched_reason(query: str, name: str, location: str) -> str:
        normalized_query = query.strip().lower()
        if normalized_query and normalized_query in name.lower():
            return "name"
        return "path"

    def _source_root(self, source: FileSourceProfile) -> Path:
        if source.workspace_root is None:
            self._raise_remote_not_supported(source)
        return source.workspace_root

    def _resolve_inside_workspace(self, path: Path) -> Path:
        candidate = Path(path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._workspace_root / candidate).resolve()
        )
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise PermissionError(
                f"Path '{resolved}' is outside workspace root '{self._workspace_root}'."
            ) from exc
        return resolved

    def _resolve_path_inside_root(self, relative_path: str | Path, root: Path) -> Path:
        raw = str(relative_path)
        if raw.startswith("\\\\"):
            raise PermissionError("Network shares are not allowed.")
        candidate = Path(relative_path)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PermissionError(
                f"Path '{relative_path}' is outside workspace root '{root}'."
            ) from exc
        return resolved

    def _build_item_ref(
        self,
        source: FileSourceProfile,
        path: Path,
        *,
        allow_missing: bool = False,
        force_item_type: ItemType | None = None,
    ) -> dict[str, Any]:
        exists = path.exists()
        if not exists and not allow_missing:
            self._raise_error("ITEM_NOT_FOUND", f"Item '{path.name}' was not found.")
        if force_item_type is not None:
            item_type = force_item_type
        elif exists:
            item_type = "folder" if path.is_dir() else "file"
        else:
            item_type = "folder" if path.suffix == "" else "file"
        return {
            "source_id": source.source_id,
            "item_type": item_type,
            "local_path": self._local_relpath(path, source),
            "remote_path": None,
            "remote_item_id": None,
            "drive_id": source.drive_id,
            "site_id": source.site_id,
            "etag": None,
            "size": path.stat().st_size if exists and path.is_file() else None,
            "modified_at": _mtime_iso(path) if exists else None,
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }

    def _resolve_item_input(
        self,
        item_ref: Mapping[str, Any] | str | None,
        *,
        default_source_id: str | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        if item_ref is None:
            self._raise_error("ITEM_NOT_FOUND", "item_ref is required.")
        if isinstance(item_ref, str):
            source = (
                self._default_local_source()
                if default_source_id is None
                else self._require_source(default_source_id)
            )
            if self._source_is_remote(source):
                return self._resolve_remote_item_input(
                    source,
                    {"remote_path": item_ref},
                    allow_missing=allow_missing,
                )
            path = self._resolve_path_inside_root(item_ref, self._source_root(source))
            return self._build_item_ref(source, path, allow_missing=allow_missing)
        source_id = item_ref.get("source_id") or default_source_id
        if not isinstance(source_id, str) or not source_id.strip():
            self._raise_error("SOURCE_NOT_FOUND", "item_ref.source_id is required.")
        source = self._require_source(source_id)
        if self._source_is_remote(source):
            return self._resolve_remote_item_input(source, item_ref, allow_missing=allow_missing)
        local_path = item_ref.get("local_path") or item_ref.get("path")
        if not isinstance(local_path, str) or not local_path.strip():
            self._raise_error("ITEM_NOT_FOUND", "item_ref.local_path is required.")
        path = self._resolve_path_inside_root(local_path, self._source_root(source))
        force_item_type = (
            item_ref.get("item_type") if isinstance(item_ref.get("item_type"), str) else None
        )
        resolved = self._build_item_ref(
            source,
            path,
            allow_missing=allow_missing,
            force_item_type=force_item_type,  # type: ignore[arg-type]
        )
        self._merge_item_ref_metadata(resolved, item_ref)
        return resolved

    def _path_from_item_ref(self, item_ref: Mapping[str, Any]) -> Path:
        source = self._require_source(str(item_ref["source_id"]))
        if self._source_is_remote(source):
            self._raise_error("ITEM_TYPE_MISMATCH", "Remote item_ref does not have a local_path.")
        local_path = item_ref.get("local_path")
        if not isinstance(local_path, str) or not local_path.strip():
            self._raise_error("ITEM_NOT_FOUND", "item_ref.local_path is required.")
        return self._resolve_path_inside_root(local_path, self._source_root(source))

    def _require_source(self, source_id: str) -> FileSourceProfile:
        source = self._sources.get(source_id)
        if source is None:
            self._raise_error("SOURCE_NOT_FOUND", f"Source '{source_id}' was not found.")
        if not source.enabled:
            self._raise_error("SOURCE_DISABLED", f"Source '{source_id}' is disabled.")
        return source

    def _require_local_source(self, source_id: str) -> FileSourceProfile:
        source = self._require_source(source_id)
        if source.source_type != "local_workspace":
            self._raise_remote_not_supported(source)
        return source

    def _default_local_source(self) -> FileSourceProfile:
        for source in self._sources.values():
            if source.source_type == "local_workspace" and source.enabled:
                return source
        self._raise_error("SOURCE_NOT_FOUND", "No enabled local_workspace source is configured.")

    def _ensure_text_read_allowed(self, item_ref: Mapping[str, Any]) -> None:
        self._ensure_extension_allowed(item_ref)
        if item_ref["item_type"] != "file":
            self._raise_error("ITEM_TYPE_MISMATCH", "Text reads require a file target.")
        if _is_secret_like(self._item_location(item_ref)):
            self._raise_error("PERMISSION_DENIED", "Reading secret-like files is denied by policy.")
        extension = self._item_extension(item_ref)
        if extension not in DEFAULT_TEXT_EXTENSIONS:
            self._raise_error(
                "ITEM_TYPE_MISMATCH",
                f"Extension '{extension}' is not supported for inline text reads.",
            )

    def _ensure_document_read_allowed(self, item_ref: Mapping[str, Any]) -> None:
        self._ensure_extension_allowed(item_ref)
        if _is_secret_like(self._item_location(item_ref)):
            self._raise_error("PERMISSION_DENIED", "Reading secret-like files is denied by policy.")

    def _ensure_text_edit_allowed(
        self,
        item_ref: Mapping[str, Any],
        *,
        create_if_missing: bool,
    ) -> None:
        source = self._require_source(str(item_ref["source_id"]))
        extension = self._item_extension(item_ref)
        if extension not in DEFAULT_TEXT_EXTENSIONS:
            self._raise_error(
                "ITEM_TYPE_MISMATCH",
                f"Extension '{extension}' is not supported for inline text edits.",
            )
        if _is_secret_like(self._item_location(item_ref)):
            self._raise_error("PERMISSION_DENIED", "Editing secret-like files is denied by policy.")
        if self._source_is_remote(source):
            if item_ref.get("remote_item_id") is None and not create_if_missing:
                self._raise_error(
                    "ITEM_NOT_FOUND",
                    f"Item '{PurePosixPath(self._item_location(item_ref)).name}' does not exist.",
                )
        else:
            path = self._path_from_item_ref(item_ref)
            if not path.exists() and not create_if_missing:
                self._raise_error("ITEM_NOT_FOUND", f"Item '{path.name}' does not exist.")
        self._ensure_extension_allowed(item_ref)

    def _ensure_extension_allowed(self, item_ref: Mapping[str, Any]) -> None:
        source = self._require_source(str(item_ref["source_id"]))
        extension = self._item_extension(item_ref)
        if extension and extension in source.denied_extensions:
            self._raise_error("EXTENSION_DENIED", f"Extension '{extension}' is denied by policy.")
        if extension and source.allowed_extensions and extension not in source.allowed_extensions:
            self._raise_error("EXTENSION_DENIED", f"Extension '{extension}' is not allowed.")

    def _require_session(self, session_id: str) -> FileEditSession:
        session = self._sessions.get(session_id)
        if session is None:
            self._raise_error("SESSION_NOT_FOUND", f"Session '{session_id}' was not found.")
        now = datetime.now(UTC)
        idle_deadline = session.last_accessed_at + timedelta(
            seconds=self._config.limits.idle_timeout_sec
        )
        if now > session.expires_at or now > idle_deadline:
            session.state = "EXPIRED"
            self._cleanup_session(session)
            self._raise_error("SESSION_EXPIRED", f"Session '{session_id}' has expired.")
        if session.state in {"COMMITTED", "CANCELED", "FAILED", "EXPIRED"}:
            self._raise_error("SESSION_NOT_FOUND", f"Session '{session_id}' is no longer active.")
        session.touch()
        return session

    def _require_text_session(self, session_id: str) -> FileEditSession:
        session = self._require_session(session_id)
        if not bool(session.metadata.get("text_session")):
            self._raise_error("ITEM_TYPE_MISMATCH", "Session is not a text edit session.")
        if session.temp_local_copy is None:
            self._raise_error("VALIDATION_FAILED", "Text session is missing a temp copy.")
        return session

    def _ensure_structural_session(
        self,
        *,
        session_id: str | None,
        item_ref: Mapping[str, Any] | str | None,
    ) -> FileEditSession:
        if session_id is not None:
            return self._require_session(session_id)
        if item_ref is None:
            self._raise_error("SESSION_NOT_FOUND", "session_id or item_ref is required.")
        resolved = self._resolve_item_input(item_ref)
        return self._create_structural_session(resolved)

    def _create_structural_session(self, item_ref: Mapping[str, Any]) -> FileEditSession:
        source = self._require_local_source(str(item_ref["source_id"]))
        if source.read_only:
            self._raise_error("SOURCE_READ_ONLY", f"Source '{source.source_id}' is read-only.")
        target_path = self._path_from_item_ref(item_ref)
        base_hash = (
            _sha256_file(target_path) if target_path.exists() and target_path.is_file() else None
        )
        base_size = (
            target_path.stat().st_size if target_path.exists() and target_path.is_file() else None
        )
        base_modified_at = _mtime_iso(target_path) if target_path.exists() else None
        session_id = uuid4().hex
        session = FileEditSession(
            session_id=session_id,
            source_id=source.source_id,
            target_item_ref=dict(item_ref),
            target_mode="local",
            base_hash=base_hash,
            base_etag=None,
            base_size=base_size,
            base_modified_at=base_modified_at,
            temp_local_copy=None,
            opened_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(seconds=self._config.limits.hard_timeout_sec),
            actor=self._actor(),
            audit_ref=self._audit_file.relative_to(self._workspace_root).as_posix(),
            initial_exists=target_path.exists(),
            item_type="folder" if target_path.is_dir() else "file",
            metadata={"text_session": False},
        )
        self._sessions[session_id] = session
        return session

    def _validate_remote_session(
        self,
        *,
        session: FileEditSession,
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        source = self._require_source(session.source_id)
        try:
            current = self._remote_client(source).resolve_item(
                remote_path=self._item_location(session.target_item_ref),
                item_id=(
                    str(session.target_item_ref["remote_item_id"])
                    if isinstance(session.target_item_ref.get("remote_item_id"), str)
                    else None
                ),
                allow_missing=not session.initial_exists,
                force_item_type=session.item_type,
            )
        except GraphFileClientError as exc:
            errors.append({"code": exc.code, "message": exc.message})
            return
        if session.initial_exists and session.base_etag and current.etag != session.base_etag:
            errors.append(
                {
                    "code": "ETAG_MISMATCH",
                    "message": "Remote target changed since the session was opened.",
                }
            )
        if not session.initial_exists and current.item_id is not None:
            errors.append(
                {
                    "code": "OVERWRITE_NOT_ALLOWED",
                    "message": "Remote target now exists and overwrite is disabled.",
                }
            )
        if session.base_etag is None and session.initial_exists:
            warnings.append(
                {
                    "code": "remote_etag_unknown",
                    "message": "Remote target does not have a known base eTag.",
                }
            )

    def _validate_session_extensions(
        self,
        *,
        session: FileEditSession,
        errors: list[dict[str, Any]],
    ) -> None:
        try:
            self._ensure_extension_allowed(session.target_item_ref)
        except FileToolError as exc:
            errors.append({"code": exc.code, "message": exc.message})

    def _commit_remote_session(
        self,
        *,
        session: FileEditSession,
        source: FileSourceProfile,
        commit_message: str | None,
        started: float,
    ) -> dict[str, Any]:
        unsupported = [
            operation.operation_type
            for operation in session.staged_operations
            if operation.operation_type
            not in {"replace_text", "patch_text", "insert_text", "append_text", "create_text_file"}
        ]
        if unsupported:
            self._raise_error(
                "REMOTE_API_ERROR",
                "Remote roundtrip currently supports text create/replace/patch operations only.",
                detail={"unsupported_operations": unsupported},
            )
        if session.temp_local_copy is None:
            self._raise_error("VALIDATION_FAILED", "Text session is missing a temp copy.")
        client = self._remote_client(source)
        if session.initial_exists and session.base_etag is not None:
            current = client.resolve_item(
                remote_path=self._item_location(session.target_item_ref),
                item_id=(
                    str(session.target_item_ref["remote_item_id"])
                    if isinstance(session.target_item_ref.get("remote_item_id"), str)
                    else None
                ),
                allow_missing=False,
                force_item_type=session.item_type,
            )
            if current.etag != session.base_etag:
                self._raise_error(
                    "ETAG_MISMATCH",
                    "Remote target changed since the session was opened.",
                    detail={"current_etag": current.etag, "base_etag": session.base_etag},
                )
        uploaded = client.upload_file(
            remote_path=self._item_location(session.target_item_ref),
            local_path=session.temp_local_copy,
            base_etag=session.base_etag,
            overwrite=self._config.policies.remote_overwrite_enabled,
        )
        final_item_ref = uploaded.to_item_ref()
        result = {
            "commit_id": uuid4().hex,
            "final_item_ref": final_item_ref,
            "backup_ref": None,
            "changed_targets": [uploaded.remote_path],
            "commit_message": commit_message,
        }
        bytes_written = session.temp_local_copy.stat().st_size
        session.state = "COMMITTED"
        session.commit_result = result
        session.backup_ref = None
        self._cleanup_session(session)
        self._audit(
            "commit_file_edit_session",
            "success",
            source_id=session.source_id,
            target_path_or_item_id=uploaded.remote_path,
            item_type=session.item_type,
            duration_ms=_duration_ms(started),
            risk_flags=[
                risk for operation in session.staged_operations for risk in operation.risk_flags
            ],
            session_id=session.session_id,
            bytes_written=bytes_written,
            remote_etag_before=session.base_etag,
            remote_etag_after=uploaded.etag,
        )
        return result

    def _read_session_text(
        self,
        session: FileEditSession,
        *,
        encoding: str | None = None,
    ) -> tuple[str, str]:
        if session.temp_local_copy is None:
            self._raise_error("VALIDATION_FAILED", "Session temp copy is missing.")
        selected_encoding = encoding or str(session.metadata.get("encoding", "utf-8"))
        text, detected = _decode_text_bytes(
            session.temp_local_copy.read_bytes(),
            encoding=selected_encoding,
        )
        return text, detected

    def _write_session_text(
        self,
        session: FileEditSession,
        text: str,
        *,
        encoding: str,
    ) -> None:
        if session.temp_local_copy is None:
            self._raise_error("VALIDATION_FAILED", "Session temp copy is missing.")
        session.temp_local_copy.write_bytes(text.encode(encoding))
        session.metadata["encoding"] = encoding
        session.metadata["newline"] = _detect_newline_style(text)

    def _ensure_inline_write_size(self, text: str) -> None:
        if len(text) > self._config.limits.max_inline_write_chars:
            self._raise_error(
                "CONTENT_TOO_LARGE",
                f"Inline write exceeds {self._config.limits.max_inline_write_chars} characters.",
            )

    def _text_risk_flags(
        self,
        session: FileEditSession,
        before_text: str,
        after_text: str,
    ) -> list[str]:
        risk_flags: list[str] = []
        if session.initial_exists and before_text != after_text:
            risk_flags.append("overwrites_existing_file")
        if session.target_mode == "remote_roundtrip" and session.base_etag is None:
            risk_flags.append("remote_etag_unknown")
        if _detect_newline_style(before_text) != _detect_newline_style(after_text):
            risk_flags.append("newline_normalization_impact")
        if _is_secret_like(self._item_location(session.target_item_ref)):
            risk_flags.append("potential_secret_file")
        if len(after_text) > self._config.limits.max_text_read_chars:
            risk_flags.append("large_file")
        return risk_flags

    def _record_operation(self, session: FileEditSession, operation: FileStagedOperation) -> None:
        session.staged_operations.append(operation)
        session.preview_summary = None
        session.validation_summary = None
        session.state = "STAGING"
        self._audit(
            f"stage_{operation.operation_type}",
            "success",
            source_id=session.source_id,
            target_path_or_item_id=operation.target_path,
            item_type=session.item_type,
            risk_flags=operation.risk_flags,
            session_id=session.session_id,
        )

    def _apply_structural_operation(  # noqa: C901
        self,
        *,
        source: FileSourceProfile,
        current_path: Path,
        session: FileEditSession,
        operation: FileStagedOperation,
        changed_targets: list[str],
    ) -> Path:
        op_type = operation.operation_type
        if op_type == "rename_item":
            destination = current_path.with_name(str(operation.metadata["new_name"]))
            if destination.exists():
                self._raise_error(
                    "OVERWRITE_NOT_ALLOWED",
                    f"Destination '{destination.name}' already exists.",
                )
            current_path.rename(destination)
            session.target_item_ref["local_path"] = self._local_relpath(destination, source)
            changed_targets.append(self._local_relpath(destination, source))
            return destination
        if op_type == "move_item":
            destination = self._resolve_path_inside_root(
                str(operation.metadata["destination_path"]),
                self._source_root(source),
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and operation.metadata.get("conflict_policy") == "fail":
                self._raise_error(
                    "OVERWRITE_NOT_ALLOWED",
                    f"Destination '{destination.name}' already exists.",
                )
            shutil.move(str(current_path), str(destination))
            session.target_item_ref["local_path"] = self._local_relpath(destination, source)
            changed_targets.append(self._local_relpath(destination, source))
            return destination
        if op_type == "copy_item":
            destination = self._resolve_path_inside_root(
                str(operation.metadata["destination_path"]),
                self._source_root(source),
            )
            if destination.exists() and not bool(operation.metadata.get("overwrite", False)):
                self._raise_error(
                    "OVERWRITE_NOT_ALLOWED",
                    f"Destination '{destination.name}' already exists.",
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if current_path.is_dir():
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(current_path, destination)
            else:
                shutil.copy2(current_path, destination)
            changed_targets.append(self._local_relpath(destination, source))
            return current_path
        if op_type == "create_folder":
            current_path.mkdir(parents=True, exist_ok=True)
            changed_targets.append(self._local_relpath(current_path, source))
            return current_path
        if op_type == "delete_item":
            if current_path.is_dir():
                shutil.rmtree(current_path)
            elif current_path.exists():
                current_path.unlink()
            changed_targets.append(self._local_relpath(current_path, source))
            return current_path
        return current_path

    def _create_backup(
        self,
        *,
        source: FileSourceProfile,
        target_path: Path,
        session: FileEditSession,
    ) -> dict[str, Any]:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"{timestamp}_{target_path.name}_{session.session_id}.bak"
        backup_path = source.backup_dir / backup_name
        shutil.copy2(target_path, backup_path)
        metadata = {
            "backup_ref": uuid4().hex,
            "source_id": source.source_id,
            "original_path": self._local_relpath(target_path, source),
            "backup_path": backup_path.relative_to(source.backup_dir).as_posix(),
            "sha256": _sha256_file(backup_path),
            "size": backup_path.stat().st_size,
            "session_id": session.session_id,
            "actor": session.actor,
            "created_at": datetime.now(UTC).isoformat(),
        }
        (source.backup_dir / f"{metadata['backup_ref']}.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return metadata

    def _create_compat_backup(self, target_path: Path) -> None:
        source = self._default_local_source()
        session = FileEditSession(
            session_id=f"compat-{uuid4().hex}",
            source_id=source.source_id,
            target_item_ref=self._build_item_ref(source, target_path),
            target_mode="local",
            base_hash=None,
            base_etag=None,
            base_size=target_path.stat().st_size,
            base_modified_at=_mtime_iso(target_path),
            temp_local_copy=None,
            opened_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
            actor=self._actor(),
        )
        self._create_backup(source=source, target_path=target_path, session=session)

    def _resolve_backup_metadata(self, backup_ref: Mapping[str, Any] | str) -> dict[str, Any]:
        reference = backup_ref.get("backup_ref") if isinstance(backup_ref, Mapping) else backup_ref
        if not isinstance(reference, str) or not reference.strip():
            self._raise_error("VALIDATION_FAILED", "backup_ref is required.")
        for source in self._sources.values():
            if source.source_type != "local_workspace":
                continue
            metadata_path = source.backup_dir / f"{reference}.json"
            if metadata_path.exists():
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
        self._raise_error("ITEM_NOT_FOUND", f"Backup ref '{reference}' was not found.")

    def _cleanup_session(self, session: FileEditSession) -> None:
        if session.temp_local_copy is not None:
            shutil.rmtree(session.temp_local_copy.parent, ignore_errors=True)

    def _local_relpath(self, path: Path, source: FileSourceProfile) -> str:
        return path.relative_to(self._source_root(source)).as_posix()

    @staticmethod
    def _actor() -> str:
        for env_name in ("FILE_MCP_ACTOR", "USERNAME", "USER"):
            value = os.getenv(env_name)
            if value and value.strip():
                return value.strip()
        return "unknown"

    def _raise_remote_not_supported(self, source: FileSourceProfile) -> None:
        self._raise_error(
            "REMOTE_API_ERROR",
            (
                f"Remote source '{source.source_id}' ({source.source_type}) is configured, "
                "but this operation is not supported for remote sources in the current build."
            ),
            suggested_action=(
                "Use local text roundtrip tools for remote files or a local_workspace source."
            ),
        )

    def _audit(
        self,
        operation: str,
        result: str,
        *,
        source_id: str | None = None,
        target_path_or_item_id: str | None = None,
        item_type: str | None = None,
        duration_ms: int | None = None,
        bytes_read: int | None = None,
        bytes_written: int | None = None,
        risk_flags: Sequence[str] | None = None,
        session_id: str | None = None,
        backup_ref: str | None = None,
        remote_etag_before: str | None = None,
        remote_etag_after: str | None = None,
    ) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "request_id": uuid4().hex,
            "actor": self._actor(),
            "source_id": source_id,
            "operation": operation,
            "target_path_or_item_id": target_path_or_item_id,
            "item_type": item_type,
            "result": result,
            "duration_ms": duration_ms,
            "bytes_read": bytes_read,
            "bytes_written": bytes_written,
            "risk_flags": list(risk_flags or []),
            "session_id": session_id,
            "backup_ref": backup_ref,
            "remote_etag_before": remote_etag_before,
            "remote_etag_after": remote_etag_after,
        }
        self._audit_file.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")

    @staticmethod
    def _raise_error(code: str, message: str, **kwargs: Any) -> None:
        raise FileToolError(code, message, **kwargs)


def _env_flag(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_extension(extension: str) -> str:
    lowered = extension.strip().lower()
    return lowered if lowered.startswith(".") else f".{lowered}"


def _matches_text(
    text: str,
    pattern: str,
    *,
    case_sensitive: bool,
    regex: bool,
) -> bool:
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(pattern, text, flags=flags) is not None
    if case_sensitive:
        return pattern in text
    return pattern.lower() in text.lower()


def _decode_text_bytes(raw_bytes: bytes, *, encoding: str | None) -> tuple[str, str]:
    candidates = (
        [encoding]
        if encoding not in (None, "", "auto")
        else [None, "utf-8-sig", "utf-8", "cp932", "shift_jis"]
    )
    for candidate in candidates:
        try:
            selected = candidate or _detect_encoding(raw_bytes)
            return raw_bytes.decode(selected), selected
        except UnicodeDecodeError:
            continue
    raise FileToolError("ENCODING_UNKNOWN", "Unable to detect or decode file encoding.")


def _detect_encoding(raw_bytes: bytes) -> str:
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def _normalize_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_newline_style(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _apply_newline_mode(text: str, *, newline_mode: str, existing_newline: str) -> str:
    normalized = _normalize_text_newlines(text)
    if newline_mode == "preserve":
        return normalized.replace("\n", existing_newline)
    if newline_mode in {"lf", "unix"}:
        return normalized
    if newline_mode in {"crlf", "windows"}:
        return normalized.replace("\n", "\r\n")
    raise FileToolError("VALIDATION_FAILED", f"Unsupported newline_mode '{newline_mode}'.")


def _apply_text_patch(  # noqa: C901
    text: str,
    *,
    patch_type: str,
    operations: Sequence[Mapping[str, Any]],
    max_regex_replace_count: int,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    updated = text
    if patch_type == "exact_replace":
        for operation in operations:
            search = _require_mapping_str(operation, "search")
            replace = str(operation.get("replace", ""))
            if search not in updated:
                raise FileToolError("VALIDATION_FAILED", f"Patch anchor '{search}' was not found.")
            count = int(operation.get("count", 1))
            updated = updated.replace(search, replace, count)
        return updated, warnings
    if patch_type == "line_replace":
        lines = updated.splitlines()
        for operation in operations:
            line_number = int(operation.get("line_number", 0))
            if line_number <= 0 or line_number > len(lines):
                raise FileToolError(
                    "VALIDATION_FAILED",
                    f"line_number {line_number} is out of range.",
                )
            lines[line_number - 1] = _require_mapping_str(operation, "content")
        return "\n".join(lines), warnings
    if patch_type == "regex_replace":
        for operation in operations:
            pattern = _require_mapping_str(operation, "pattern")
            replacement = str(operation.get("replacement", ""))
            count = int(operation.get("count", 0))
            updated, replaced = re.subn(pattern, replacement, updated, count=count)
            if replaced == 0:
                raise FileToolError("VALIDATION_FAILED", f"Regex '{pattern}' did not match.")
            if replaced > max_regex_replace_count:
                raise FileToolError(
                    "VALIDATION_FAILED",
                    "Regex replacement count exceeded policy threshold.",
                )
        return updated, warnings
    raise FileToolError("VALIDATION_FAILED", f"Unsupported patch_type '{patch_type}'.")


def _insert_text(
    text: str,
    *,
    content: str,
    position: str,
    byte_offset: int | None,
    line_number: int | None,
    encoding: str,
) -> str:
    if position == "start":
        return f"{content}{text}"
    if position == "end":
        return f"{text}{content}"
    if position == "byte_offset":
        if byte_offset is None or byte_offset < 0:
            raise FileToolError("VALIDATION_FAILED", "byte_offset must be >= 0.")
        raw = text.encode(encoding)
        inserted = raw[:byte_offset] + content.encode(encoding) + raw[byte_offset:]
        return inserted.decode(encoding)
    if position == "line_number":
        if line_number is None or line_number <= 0:
            raise FileToolError("VALIDATION_FAILED", "line_number must be > 0.")
        lines = text.splitlines(keepends=True)
        index = min(line_number - 1, len(lines))
        lines.insert(index, content)
        return "".join(lines)
    raise FileToolError("VALIDATION_FAILED", f"Unsupported position '{position}'.")


def _diff_summary(before: str, after: str) -> dict[str, Any]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = 0
    removed = 0
    modified = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            modified += max(i2 - i1, j2 - j1)
    unified_diff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )
    return {
        "line_additions": added,
        "line_removals": removed,
        "line_modifications": modified,
        "character_count_delta": len(after) - len(before),
        "newline_normalization_impact": _detect_newline_style(before)
        != _detect_newline_style(after),
        "encoding_change_impact": False,
        "unified_diff": unified_diff[:200],
    }


def _extract_docx_text(path: Path) -> str:
    with ZipFile(path) as archive:
        xml_text = archive.read("word/document.xml")
    root = ET.fromstring(xml_text)
    texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
    return "\n".join(part for part in texts if part)


def _extract_pptx_text(path: Path) -> str:
    texts: list[str] = []
    with ZipFile(path) as archive:
        for name in sorted(
            item
            for item in archive.namelist()
            if item.startswith("ppt/slides/slide") and item.endswith(".xml")
        ):
            root = ET.fromstring(archive.read(name))
            texts.extend(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
    return "\n".join(part for part in texts if part)


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError as exc:
        raise FileToolError(
            "ITEM_TYPE_MISMATCH",
            "PDF extraction requires optional dependency 'pypdf'.",
            suggested_action='Install with `pip install "orchestra-agent[mcp-server]"`.',
        ) from exc
    reader = PdfReader(str(path))
    parts = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(parts)


def _extract_rtf_text(text: str) -> str:
    cleaned = re.sub(r"\\[a-z]+\d* ?", "", text)
    cleaned = cleaned.replace("{", "").replace("}", "")
    return cleaned


def _extract_odt_text(path: Path) -> str:
    with ZipFile(path) as archive:
        xml_text = archive.read("content.xml")
    root = ET.fromstring(xml_text)
    texts = [node.text or "" for node in root.iter() if node.text]
    return "\n".join(part for part in texts if part)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _duration_ms(started: float) -> int:
    return int((time_module.perf_counter() - started) * 1000)


def _has_text_operations(session: FileEditSession) -> bool:
    return any(
        operation.operation_type
        in {"replace_text", "patch_text", "insert_text", "append_text", "create_text_file"}
        for operation in session.staged_operations
    )


def _unique_list(values: Sequence[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _require_mapping_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise FileToolError("VALIDATION_FAILED", f"{key} must be a non-empty string.")
    return value


def _is_secret_like(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if any(normalized.lower().endswith(extension) for extension in DEFAULT_DENIED_EXTENSIONS):
        return True
    return any(pattern.search(normalized) for pattern in _SECRET_NAME_PATTERNS)


def _optional_iso(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _next_numbered_copy(path: Path) -> Path:
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _next_numbered_remote_path(remote_path: str) -> str:
    current = PurePosixPath(remote_path)
    match = re.search(r"^(.*) \((\d+)\)$", current.stem)
    if match:
        stem = match.group(1)
        index = int(match.group(2)) + 1
    else:
        stem = current.stem
        index = 2
    suffix = current.suffix
    parent = current.parent.as_posix()
    candidate = f"{stem} ({index}){suffix}"
    if parent in {"", "."}:
        return f"/{candidate}"
    return f"{parent.rstrip('/')}/{candidate}"
