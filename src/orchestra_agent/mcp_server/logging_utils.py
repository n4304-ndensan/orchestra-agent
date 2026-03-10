from __future__ import annotations

import json
import logging
import os
import traceback
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER_NAME = "orchestra_agent.mcp_server"
_MAX_STRING_LENGTH = 240
_MAX_COLLECTION_ITEMS = 12
_MAX_DEPTH = 3


def configure_mcp_logging() -> None:
    root_logger = logging.getLogger(_LOGGER_NAME)
    level_name = os.getenv("ORCHESTRA_MCP_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if getattr(root_logger, "_orchestra_configured", False):
        root_logger.setLevel(level)
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
    root_logger.propagate = False
    root_logger._orchestra_configured = True  # type: ignore[attr-defined]


def get_mcp_logger(name: str) -> logging.Logger:
    configure_mcp_logging()
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event_type: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        **{key: _preview_value(value) for key, value in fields.items()},
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def log_exception(
    logger: logging.Logger,
    event_type: str,
    exc: Exception,
    *,
    level: int = logging.ERROR,
    **fields: Any,
) -> None:
    log_event(
        logger,
        event_type,
        level=level,
        error=str(exc),
        error_type=type(exc).__name__,
        traceback="".join(traceback.format_exception(exc)),
        **fields,
    )


def _preview_value(value: Any, *, depth: int = 0) -> Any:
    scalar_preview = _preview_scalar(value)
    if scalar_preview is not None:
        return scalar_preview
    if depth >= _MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        return _preview_mapping(value, depth=depth + 1)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return _preview_sequence(value, depth=depth + 1)
    return repr(value)


def _preview_scalar(value: Any) -> Any | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_LENGTH:
            return value
        return f"{value[:_MAX_STRING_LENGTH - 3]}..."
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return None


def _preview_mapping(value: Mapping[Any, Any], *, depth: int) -> dict[str, Any]:
    items = list(value.items())
    preview = {
        str(key): _preview_value(item, depth=depth)
        for key, item in items[:_MAX_COLLECTION_ITEMS]
    }
    remaining = len(items) - _MAX_COLLECTION_ITEMS
    if remaining > 0:
        preview["__truncated_items__"] = remaining
    return preview


def _preview_sequence(value: Sequence[Any], *, depth: int) -> list[Any]:
    items = [_preview_value(item, depth=depth) for item in value[:_MAX_COLLECTION_ITEMS]]
    remaining = len(value) - _MAX_COLLECTION_ITEMS
    if remaining > 0:
        items.append(f"... +{remaining} more")
    return items
