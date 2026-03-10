from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

type RuntimeActionType = Literal[
    "call_mcp_tool",
    "request_file_attachments",
    "write_file",
    "finish",
]

STEP_RUNTIME_PROTOCOL_VERSION = 2


@dataclass(slots=True, frozen=True)
class CallMcpToolAction:
    tool_ref: str
    input: dict[str, Any]
    extensions: dict[str, Any] = field(default_factory=dict)
    type: Literal["call_mcp_tool"] = "call_mcp_tool"


@dataclass(slots=True, frozen=True)
class RequestFileAttachmentsAction:
    paths: list[str]
    reason: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)
    type: Literal["request_file_attachments"] = "request_file_attachments"


@dataclass(slots=True, frozen=True)
class WriteFileAction:
    path: str
    content: str
    extensions: dict[str, Any] = field(default_factory=dict)
    type: Literal["write_file"] = "write_file"


@dataclass(slots=True, frozen=True)
class FinishAction:
    result: dict[str, Any]
    extensions: dict[str, Any] = field(default_factory=dict)
    type: Literal["finish"] = "finish"


type RuntimeAction = (
    CallMcpToolAction | RequestFileAttachmentsAction | WriteFileAction | FinishAction
)


def parse_runtime_action(raw_action: Any) -> RuntimeAction:
    if not isinstance(raw_action, dict):
        raise ValueError("LLM step runtime action must be an object.")

    raw_type = raw_action.get("type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError("LLM step runtime action requires string 'type'.")

    action_type = raw_type.strip()
    if action_type == "call_mcp_tool":
        return CallMcpToolAction(
            tool_ref=_required_str(raw_action, "tool_ref"),
            input=_required_dict(raw_action, "input"),
            extensions=_extensions(raw_action, {"type", "tool_ref", "input"}),
        )
    if action_type == "request_file_attachments":
        return RequestFileAttachmentsAction(
            paths=_required_str_list(raw_action, "paths"),
            reason=_optional_str(raw_action.get("reason")),
            extensions=_extensions(raw_action, {"type", "paths", "reason"}),
        )
    if action_type == "write_file":
        return WriteFileAction(
            path=_required_str(raw_action, "path"),
            content=_required_str(raw_action, "content"),
            extensions=_extensions(raw_action, {"type", "path", "content"}),
        )
    if action_type == "finish":
        return FinishAction(
            result=_required_dict(raw_action, "result"),
            extensions=_extensions(raw_action, {"type", "result"}),
        )

    raise ValueError(f"Unsupported LLM step runtime action type: {action_type}")


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"LLM step runtime action requires string '{key}'.")
    return value


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"LLM step runtime action requires object '{key}'.")
    return value


def _required_str_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"LLM step runtime action requires string array '{key}'.")
    return list(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("LLM step runtime action optional string fields must be strings.")
    return value


def _extensions(payload: dict[str, Any], reserved_keys: set[str]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in payload.items()
        if key not in reserved_keys
    }
