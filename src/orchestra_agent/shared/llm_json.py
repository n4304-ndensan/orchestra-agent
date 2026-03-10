from __future__ import annotations

import json
from typing import Any

from orchestra_agent.shared.error_handling import text_preview


def extract_json_payload(raw_text: str, *, label: str) -> Any:
    stripped = raw_text.strip()
    last_error: json.JSONDecodeError | None = None

    for candidate in _json_candidates(stripped):
        for payload in _repair_candidates(candidate):
            try:
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                last_error = exc

    preview = text_preview(stripped)
    if last_error is None:
        raise ValueError(f"{label} is not valid JSON. preview={preview}")
    raise ValueError(f"{label} is not valid JSON. preview={preview}; parse_error={last_error}")


def _json_candidates(stripped: str) -> list[str]:
    candidates: list[str] = []
    if stripped:
        candidates.append(stripped)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = stripped[start : end + 1]
        if candidate not in candidates:
            candidates.append(candidate)

    return candidates


def _repair_candidates(candidate: str) -> list[str]:
    repaired = _escape_invalid_backslashes(candidate)
    if repaired == candidate:
        return [candidate]
    return [candidate, repaired]


def _escape_invalid_backslashes(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escaped = False
    changed = False

    for index, char in enumerate(text):
        if not in_string:
            chars.append(char)
            if char == '"':
                in_string = True
            continue

        if escaped:
            chars.append(char)
            escaped = False
            continue

        if char == "\\":
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if next_char in {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}:
                chars.append(char)
                escaped = True
            else:
                chars.append("\\\\")
                changed = True
            continue

        chars.append(char)
        if char == '"':
            in_string = False

    if not changed:
        return text
    return "".join(chars)
