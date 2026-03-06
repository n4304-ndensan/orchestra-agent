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

    def create_snapshot(self, scope: BackupScope, metadata: dict[str, Any] | None = None) -> str:
        snapshot_ref = f"snap-{uuid4().hex}"
        snapshot_dir = self._base_dir / snapshot_ref
        snapshot_dir.mkdir(parents=True, exist_ok=False)

        payload = {
            "snapshot_ref": snapshot_ref,
            "scope": scope.value,
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": metadata or {},
        }

        if scope == BackupScope.FILE:
            source_file = self._extract_file_path(payload["metadata"])
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

    @staticmethod
    def _copy_tree(source: Path, destination: Path) -> None:
        def ignore(_: str, names: list[str]) -> set[str]:
            ignored = {".git", ".venv", ".venv-uv", ".uv-cache", "__pycache__", ".mypy_cache"}
            return {name for name in names if name in ignored}

        shutil.copytree(source, destination, dirs_exist_ok=False, ignore=ignore)

    @staticmethod
    def _restore_tree(source: Path, destination: Path) -> None:
        for root, _, files in source.walk():
            relative = root.relative_to(source)
            target_root = destination / relative
            target_root.mkdir(parents=True, exist_ok=True)
            for file_name in files:
                shutil.copy2(root / file_name, target_root / file_name)
