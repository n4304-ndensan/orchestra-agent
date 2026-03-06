from __future__ import annotations

from pathlib import Path
from typing import Any


class WorkspaceFileService:
    def __init__(self, workspace_root: Path, max_read_bytes: int = 1_000_000) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be greater than zero.")
        self._workspace_root = workspace_root.resolve()
        self._max_read_bytes = max_read_bytes

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def list_entries(self, relative_path: str = ".") -> list[dict[str, Any]]:
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
        return entries

    def read_text(self, relative_path: str, encoding: str = "utf-8") -> str:
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
        return target.read_text(encoding=encoding)

    def write_text(
        self,
        relative_path: str,
        content: str,
        overwrite: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        target = self._resolve_within_workspace(relative_path)
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"File '{relative_path}' already exists. Set overwrite=True to replace it."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return {
            "path": target.relative_to(self._workspace_root).as_posix(),
            "bytes": target.stat().st_size,
        }

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
