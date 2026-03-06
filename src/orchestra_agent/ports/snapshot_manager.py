from __future__ import annotations

from typing import Any, Protocol

from orchestra_agent.domain.enums import BackupScope


class ISnapshotManager(Protocol):
    def create_snapshot(self, scope: BackupScope, metadata: dict[str, Any] | None = None) -> str:
        ...

    def restore_snapshot(self, snapshot_ref: str) -> None:
        ...

