from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra_agent.ports.audit_logger import IAuditLogger


class FilesystemAuditLogger(IAuditLogger):
    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._root_dir / "events.ndjson"
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> None:
        event_with_time = {
            "timestamp": datetime.now(UTC).isoformat(),
            **event,
        }
        encoded = json.dumps(event_with_time, ensure_ascii=False)
        with self._lock, self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")

    def list_events(
        self,
        *,
        run_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._events_path.is_file():
            return []

        events: list[dict[str, Any]] = []
        for line in self._events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                continue
            if run_id is not None and parsed.get("run_id") != run_id:
                continue
            events.append(parsed)

        if limit is None or limit >= len(events):
            return events
        return events[-limit:]
