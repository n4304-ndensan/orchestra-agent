from __future__ import annotations

from orchestra_agent.adapters import InMemoryAuditLogger
from orchestra_agent.observability import (
    LoggingLlmClient,
    LoggingMcpClient,
    bind_observation_context,
)
from orchestra_agent.ports import LlmGenerateRequest, LlmMessage


class DummyLlmClient:
    def generate(self, request: LlmGenerateRequest) -> str:
        return '{"status":"ok"}'


class DummyMcpClient:
    def list_tools(self) -> list[str]:
        return ["fs_write_text"]

    def call_tool(self, tool_ref: str, input: dict[str, object]) -> dict[str, object]:
        return {"tool_ref": tool_ref, "input": input, "written": True}


def test_logging_llm_client_records_request_and_response() -> None:
    audit_logger = InMemoryAuditLogger()
    client = LoggingLlmClient(DummyLlmClient(), audit_logger)
    request = LlmGenerateRequest(
        messages=(
            LlmMessage(role="system", content="system"),
            LlmMessage(role="user", content="hello"),
        ),
        response_format="json_object",
        temperature=0.2,
        max_tokens=123,
    )

    with bind_observation_context(run_id="run-1", step_id="write_summary"):
        response = client.generate(request)

    assert response == '{"status":"ok"}'
    assert audit_logger.events[0]["event_type"] == "llm_request"
    assert audit_logger.events[0]["run_id"] == "run-1"
    assert audit_logger.events[0]["request"]["messages"][1]["content"] == "hello"
    assert audit_logger.events[1]["event_type"] == "llm_response"
    assert audit_logger.events[1]["step_id"] == "write_summary"


def test_logging_mcp_client_records_tool_call_and_result() -> None:
    audit_logger = InMemoryAuditLogger()
    client = LoggingMcpClient(DummyMcpClient(), audit_logger)

    with bind_observation_context(run_id="run-2", step_id="save_file"):
        result = client.call_tool("fs_write_text", {"path": "notes.txt", "content": "done"})

    assert result["written"] is True
    assert audit_logger.events[0]["event_type"] == "mcp_tool_call"
    assert audit_logger.events[0]["tool_ref"] == "fs_write_text"
    assert audit_logger.events[0]["run_id"] == "run-2"
    assert audit_logger.events[1]["event_type"] == "mcp_tool_result"
    assert audit_logger.events[1]["result"]["written"] is True
