from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestra_agent.domain.enums import BackupScope
from orchestra_agent.ports.snapshot_manager import ISnapshotManager


class FilesystemSnapshotManager(ISnapshotManager):
    def __init__(self, base_snapshot_dir: Path, workspace_root: Path | None = None) -> None:
        self._base_dir = base_snapshot_dir
        self._workspace_root = workspace_root or Path.cwd()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._ignored_names = {
            ".git",
            ".mypy_cache",
            ".orchestra_snapshots",
            ".orchestra_state",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            ".venv-uv",
            ".uv-cache",
            "__pycache__",
        }

    def create_snapshot(self, scope: BackupScope, metadata: dict[str, Any] | None = None) -> str:
        snapshot_ref = f"snap-{uuid4().hex}"
        snapshot_dir = self._base_dir / snapshot_ref
        snapshot_dir.mkdir(parents=True, exist_ok=False)

        metadata_payload: dict[str, Any] = metadata or {}
        payload: dict[str, Any] = {
            "snapshot_ref": snapshot_ref,
            "scope": scope.value,
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": metadata_payload,
        }

        if scope == BackupScope.FILE:
            source_file = self._extract_file_path(metadata_payload)
            if source_file is None:
                raise ValueError(
                    "FILE snapshot requires metadata containing 'file' or 'file_path'."
                )
            source = Path(source_file)
            if not source.is_file():
                raise FileNotFoundError(f"Snapshot source file not found: {source}.")
            copied_file = snapshot_dir / source.name
            shutil.copy2(source, copied_file)
            payload["restore_target"] = str(source)
            payload["snapshot_file"] = str(copied_file)
        elif scope in (BackupScope.WORKSPACE, BackupScope.FULL):
            copied_workspace = snapshot_dir / "workspace"
            self._copy_tree(self._workspace_root, copied_workspace)
            payload["restore_target"] = str(self._workspace_root)
            payload["snapshot_workspace"] = str(copied_workspace)

        (snapshot_dir / "metadata.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return snapshot_ref

    def restore_snapshot(self, snapshot_ref: str) -> None:
        snapshot_dir = self._base_dir / snapshot_ref
        metadata_path = snapshot_dir / "metadata.json"
        if not metadata_path.is_file():
            raise FileNotFoundError(f"Snapshot metadata not found for '{snapshot_ref}'.")

        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        scope = BackupScope(payload["scope"])

        if scope == BackupScope.NONE:
            return

        if scope == BackupScope.FILE:
            snapshot_file = Path(payload["snapshot_file"])
            target = Path(payload["restore_target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot_file, target)
            return

        snapshot_workspace = Path(payload["snapshot_workspace"])
        target_workspace = Path(payload["restore_target"])
        self._restore_tree(snapshot_workspace, target_workspace)

    @staticmethod
    def _extract_file_path(metadata: dict[str, Any]) -> str | None:
        file_path = metadata.get("file_path")
        if isinstance(file_path, str):
            return file_path
        file_name = metadata.get("file")
        if isinstance(file_name, str):
            return file_name
        return None

    def _copy_tree(self, source: Path, destination: Path) -> None:
        relative_snapshot_root = self._relative_snapshot_root(source)

        def ignore(current_dir: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name in self._ignored_names}
            relative_current = self._relative_to_root(Path(current_dir), source)
            if (
                relative_snapshot_root is not None
                and relative_current is not None
                and relative_current == relative_snapshot_root.parent
            ):
                ignored.add(relative_snapshot_root.name)
            return ignored

        shutil.copytree(source, destination, dirs_exist_ok=False, ignore=ignore)

    def _restore_tree(self, source: Path, destination: Path) -> None:
        relative_snapshot_root = self._relative_snapshot_root(destination)
        self._remove_extra_destination_entries(
            source=source,
            destination=destination,
            relative_snapshot_root=relative_snapshot_root,
        )

        for root, _, files in source.walk():
            relative = root.relative_to(source)
            target_root = destination / relative
            target_root.mkdir(parents=True, exist_ok=True)
            for file_name in files:
                shutil.copy2(root / file_name, target_root / file_name)

    def _remove_extra_destination_entries(
        self,
        source: Path,
        destination: Path,
        relative_snapshot_root: Path | None,
    ) -> None:
        for root, dirs, files in destination.walk(top_down=False):
            relative_root = root.relative_to(destination)
            if self._should_ignore_relative(relative_root, relative_snapshot_root):
                continue

            source_root = source / relative_root
            self._remove_extra_files(root, source_root, files)
            self._remove_extra_dirs(
                root=root,
                source=source,
                destination=destination,
                dirs=dirs,
                relative_snapshot_root=relative_snapshot_root,
            )

    def _remove_extra_files(self, root: Path, source_root: Path, files: list[str]) -> None:
        for file_name in files:
            if file_name in self._ignored_names:
                continue
            destination_file = root / file_name
            source_file = source_root / file_name
            if not source_file.exists():
                destination_file.unlink()

    def _remove_extra_dirs(
        self,
        *,
        root: Path,
        source: Path,
        destination: Path,
        dirs: list[str],
        relative_snapshot_root: Path | None,
    ) -> None:
        for dir_name in dirs:
            if dir_name in self._ignored_names:
                continue
            destination_dir = root / dir_name
            relative_dir = destination_dir.relative_to(destination)
            if self._should_ignore_relative(relative_dir, relative_snapshot_root):
                continue
            if not (source / relative_dir).exists():
                shutil.rmtree(destination_dir)

    def _relative_snapshot_root(self, root: Path) -> Path | None:
        return self._relative_to_root(self._base_dir, root)

    @staticmethod
    def _relative_to_root(path: Path, root: Path) -> Path | None:
        try:
            return path.resolve().relative_to(root.resolve())
        except ValueError:
            return None

    def _should_ignore_relative(
        self,
        relative_path: Path,
        relative_snapshot_root: Path | None,
    ) -> bool:
        if any(part in self._ignored_names for part in relative_path.parts):
            return True
        if relative_snapshot_root is None:
            return False
        return (
            relative_path == relative_snapshot_root
            or relative_snapshot_root in relative_path.parents
        )
