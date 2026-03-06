from __future__ import annotations

from typing import Any, Protocol


class IAuditLogger(Protocol):
    def record(self, event: dict[str, Any]) -> None:
        ...

