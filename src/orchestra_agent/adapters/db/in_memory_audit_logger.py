from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from orchestra_agent.ports.audit_logger import IAuditLogger


class InMemoryAuditLogger(IAuditLogger):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: dict[str, Any]) -> None:
        event_with_time = {
            "timestamp": datetime.now(UTC).isoformat(),
            **event,
        }
        self.events.append(deepcopy(event_with_time))

