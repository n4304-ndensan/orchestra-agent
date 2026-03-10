from __future__ import annotations

import pytest

from orchestra_agent.shared.llm_step_runtime_protocol import (
    CallMcpToolAction,
    FinishAction,
    RequestFileAttachmentsAction,
    WriteFileAction,
    parse_runtime_action,
)


def test_parse_runtime_action_supports_call_mcp_tool_with_extensions() -> None:
    action = parse_runtime_action(
        {
            "type": "call_mcp_tool",
            "tool_ref": "excel.read_sheet",
            "input": {"file": "sales.xlsx", "sheet": "Sheet1"},
            "retry_hint": "safe-to-retry",
        }
    )

    assert isinstance(action, CallMcpToolAction)
    assert action.tool_ref == "excel.read_sheet"
    assert action.input == {"file": "sales.xlsx", "sheet": "Sheet1"}
    assert action.extensions == {"retry_hint": "safe-to-retry"}


def test_parse_runtime_action_requires_finish_result() -> None:
    with pytest.raises(ValueError, match="requires object 'result'"):
        parse_runtime_action({"type": "finish"})


def test_parse_runtime_action_supports_finish_extensions() -> None:
    action = parse_runtime_action(
        {
            "type": "finish",
            "result": {"summary": "done"},
            "handoff": {"kind": "step_result"},
        }
    )

    assert isinstance(action, FinishAction)
    assert action.result == {"summary": "done"}
    assert action.extensions == {"handoff": {"kind": "step_result"}}


def test_parse_runtime_action_supports_attachment_requests() -> None:
    action = parse_runtime_action(
        {
            "type": "request_file_attachments",
            "paths": ["docs/spec.md"],
            "reason": "Need the spec",
        }
    )

    assert isinstance(action, RequestFileAttachmentsAction)
    assert action.paths == ["docs/spec.md"]
    assert action.reason == "Need the spec"


def test_parse_runtime_action_supports_write_file() -> None:
    action = parse_runtime_action(
        {
            "type": "write_file",
            "path": "output/report.txt",
            "content": "done",
        }
    )

    assert isinstance(action, WriteFileAction)
    assert action.path == "output/report.txt"
    assert action.content == "done"
