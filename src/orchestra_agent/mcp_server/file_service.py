from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from orchestra_agent.mcp_server.logging_utils import get_mcp_logger, log_event

logger = get_mcp_logger(__name__)


class WorkspaceFileService:
    def __init__(self, workspace_root: Path, max_read_bytes: int = 1_000_000) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be greater than zero.")
        self._workspace_root = workspace_root.resolve()
        self._max_read_bytes = max_read_bytes
        log_event(
            logger,
            "file_service_initialized",
            workspace_root=self._workspace_root,
            max_read_bytes=max_read_bytes,
        )

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def list_entries(self, relative_path: str = ".") -> list[dict[str, Any]]:
        log_event(logger, "fs_list_entries_started", path=relative_path)
        target_dir = self._resolve_within_workspace(relative_path)
        if not target_dir.exists():
            raise FileNotFoundError(f"Directory '{relative_path}' does not exist.")
        if not target_dir.is_dir():
            raise NotADirectoryError(f"Path '{relative_path}' is not a directory.")

        entries: list[dict[str, Any]] = []
        sorted_children = sorted(
            target_dir.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
        for child in sorted_children:
            child_path = child.relative_to(self._workspace_root).as_posix()
            size: int | None = child.stat().st_size if child.is_file() else None
            entries.append(
                {
                    "name": child.name,
                    "path": child_path,
                    "is_dir": child.is_dir(),
                    "size": size,
                }
            )
        log_event(logger, "fs_list_entries_succeeded", path=relative_path, entry_count=len(entries))
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
        log_event(
            logger,
            "fs_find_entries_started",
            path=path,
            pattern=pattern,
            case_sensitive=case_sensitive,
            regex=regex,
            include_dirs=include_dirs,
            max_results=max_results,
        )
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero.")
        target_dir = self._resolve_within_workspace(path)
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
            if not self._matches_text(
                text=relative_candidate,
                pattern=pattern,
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

        result = {
            "path": target_dir.relative_to(self._workspace_root).as_posix(),
            "pattern": pattern,
            "matches": matches,
            "truncated": truncated,
        }
        log_event(
            logger,
            "fs_find_entries_succeeded",
            path=path,
            pattern=pattern,
            match_count=len(matches),
            truncated=truncated,
        )
        return result

    def read_text(self, relative_path: str, encoding: str = "utf-8") -> str:
        log_event(logger, "fs_read_text_started", path=relative_path, encoding=encoding)
        target = self._resolve_within_workspace(relative_path)
        if not target.exists():
            raise FileNotFoundError(f"File '{relative_path}' does not exist.")
        if not target.is_file():
            raise IsADirectoryError(f"Path '{relative_path}' is not a file.")
        file_size = target.stat().st_size
        if file_size > self._max_read_bytes:
            raise ValueError(
                f"File '{relative_path}' is too large ({file_size} bytes). "
                f"Limit is {self._max_read_bytes} bytes."
            )
        content = target.read_text(encoding=encoding)
        log_event(
            logger,
            "fs_read_text_succeeded",
            path=relative_path,
            encoding=encoding,
            bytes=file_size,
        )
        return content

    def grep_text(
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
        log_event(
            logger,
            "fs_grep_text_started",
            path=path,
            pattern=pattern,
            case_sensitive=case_sensitive,
            regex=regex,
            file_glob=file_glob,
            max_results=max_results,
            encoding=encoding,
        )
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero.")
        target_dir = self._resolve_within_workspace(path)
        if not target_dir.exists():
            raise FileNotFoundError(f"Directory '{path}' does not exist.")
        if not target_dir.is_dir():
            raise NotADirectoryError(f"Path '{path}' is not a directory.")

        matches: list[dict[str, Any]] = []
        truncated = False
        candidate_iter = target_dir.rglob(file_glob) if file_glob else target_dir.rglob("*")
        for candidate in sorted(candidate_iter):
            if not candidate.is_file():
                continue
            grep_matches = self._grep_file(
                file_path=candidate,
                pattern=pattern,
                case_sensitive=case_sensitive,
                regex=regex,
                max_results=max_results - len(matches),
                encoding=encoding,
            )
            if not grep_matches:
                continue
            matches.extend(grep_matches)
            if len(matches) >= max_results:
                truncated = True
                break

        result = {
            "path": target_dir.relative_to(self._workspace_root).as_posix(),
            "pattern": pattern,
            "matches": matches,
            "truncated": truncated,
        }
        log_event(
            logger,
            "fs_grep_text_succeeded",
            path=path,
            pattern=pattern,
            match_count=len(matches),
            truncated=truncated,
        )
        return result

    def write_text(
        self,
        relative_path: str,
        content: str,
        overwrite: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        log_event(
            logger,
            "fs_write_text_started",
            path=relative_path,
            overwrite=overwrite,
            encoding=encoding,
            bytes=len(content.encode(encoding)),
        )
        target = self._resolve_within_workspace(relative_path)
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"File '{relative_path}' already exists. Set overwrite=True to replace it."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        result = {
            "path": target.relative_to(self._workspace_root).as_posix(),
            "bytes": target.stat().st_size,
        }
        log_event(logger, "fs_write_text_succeeded", path=relative_path, result=result)
        return result

    def copy_file(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        log_event(
            logger,
            "fs_copy_file_started",
            source=source_path,
            destination=destination_path,
            overwrite=overwrite,
        )
        source = self._resolve_within_workspace(source_path)
        destination = self._resolve_within_workspace(destination_path)

        if source == destination:
            raise ValueError("source_path and destination_path must be different.")
        if not source.exists():
            raise FileNotFoundError(f"File '{source_path}' does not exist.")
        if not source.is_file():
            raise IsADirectoryError(f"Path '{source_path}' is not a file.")
        if destination.exists():
            if destination.is_dir():
                raise IsADirectoryError(f"Path '{destination_path}' is a directory.")
            if not overwrite:
                raise FileExistsError(
                    f"File '{destination_path}' already exists. "
                    "Set overwrite=True to replace it."
                )

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        result = {
            "source": source.relative_to(self._workspace_root).as_posix(),
            "destination": destination.relative_to(self._workspace_root).as_posix(),
            "bytes": destination.stat().st_size,
        }
        log_event(
            logger,
            "fs_copy_file_succeeded",
            source=source_path,
            destination=destination_path,
        )
        return result

    def _resolve_within_workspace(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        resolved = raw.resolve() if raw.is_absolute() else (self._workspace_root / raw).resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise PermissionError(
                f"Path '{relative_path}' is outside workspace root '{self._workspace_root}'."
            ) from exc
        return resolved

    def _grep_file(
        self,
        file_path: Path,
        pattern: str,
        *,
        case_sensitive: bool,
        regex: bool,
        max_results: int,
        encoding: str,
    ) -> list[dict[str, Any]]:
        if max_results <= 0:
            return []
        if file_path.stat().st_size > self._max_read_bytes:
            return []
        try:
            content = file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            return []

        matches: list[dict[str, Any]] = []
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not self._matches_text(
                text=line,
                pattern=pattern,
                case_sensitive=case_sensitive,
                regex=regex,
            ):
                continue
            matches.append(
                {
                    "path": file_path.relative_to(self._workspace_root).as_posix(),
                    "line_number": line_number,
                    "line": line,
                }
            )
            if len(matches) >= max_results:
                break
        return matches

    @staticmethod
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
