from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def mapping_preview(payload: Mapping[str, Any], max_items: int = 4) -> str:
    if not payload:
        return ""
    parts: list[str] = []
    keys = list(payload.keys())
    for key in keys[:max_items]:
        parts.append(f"{key}={value_preview(payload[key])}")
    remaining = len(keys) - max_items
    if remaining > 0:
        parts.append(f"+{remaining} more")
    return ", ".join(parts)


def value_preview(value: Any) -> str:
    if isinstance(value, Mapping):
        return "{" + mapping_preview(value, max_items=2) + "}"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        head = ", ".join(value_preview(item) for item in value[:3])
        if len(value) > 3:
            head = f"{head}, +{len(value) - 3} more"
        return f"[{head}]"
    if isinstance(value, str):
        sanitized = value.replace("\r", " ").replace("\n", " ").strip()
        return sanitized if len(sanitized) <= 72 else f"{sanitized[:69]}..."
    return json.dumps(value, ensure_ascii=False)
