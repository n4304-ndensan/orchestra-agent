from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from orchestra_agent.ports import IAuditLogger, ILlmClient, IMcpClient, LlmGenerateRequest

_observation_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "orchestra_agent_observation_context",
    default=None,
)


def current_observation_context() -> dict[str, Any]:
    current = _observation_context.get()
    if current is None:
        return {}
    return dict(current)


def enrich_observation_event(event: dict[str, Any]) -> dict[str, Any]:
    context = current_observation_context()
    if not context:
        return event
    return {**context, **event}


@contextmanager
def bind_observation_context(**context: Any) -> Iterator[None]:
    merged = current_observation_context()
    merged.update({key: value for key, value in context.items() if value is not None})
    token = _observation_context.set(merged)
    try:
        yield
    finally:
        _observation_context.reset(token)


class LoggingLlmClient(ILlmClient):
    def __init__(
        self,
        inner: ILlmClient,
        audit_logger: IAuditLogger,
        client_name: str | None = None,
    ) -> None:
        self._inner = inner
        self._audit_logger = audit_logger
        self._client_name = client_name or type(inner).__name__

    def generate(self, request: LlmGenerateRequest) -> str:
        request_id = f"llm-{uuid4().hex[:10]}"
        request_event = {
            "event_type": "llm_request",
            "request_id": request_id,
            "client": self._client_name,
            "request": _serialize_llm_request(request),
        }
        self._audit_logger.record(enrich_observation_event(request_event))
        try:
            response = self._inner.generate(request)
        except Exception as exc:  # noqa: BLE001
            self._audit_logger.record(
                enrich_observation_event(
                    {
                        "event_type": "llm_error",
                        "request_id": request_id,
                        "client": self._client_name,
                        "error": str(exc),
                    }
                )
            )
            raise
        self._audit_logger.record(
            enrich_observation_event(
                {
                    "event_type": "llm_response",
                    "request_id": request_id,
                    "client": self._client_name,
                    "response": response,
                }
            )
        )
        return response

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LoggingMcpClient(IMcpClient):
    def __init__(
        self,
        inner: IMcpClient,
        audit_logger: IAuditLogger,
        client_name: str | None = None,
    ) -> None:
        self._inner = inner
        self._audit_logger = audit_logger
        self._client_name = client_name or type(inner).__name__

    def list_tools(self) -> list[str]:
        return self._inner.list_tools()

    def call_tool(self, tool_ref: str, input: dict[str, Any]) -> dict[str, Any]:
        call_id = f"mcp-{uuid4().hex[:10]}"
        self._audit_logger.record(
            enrich_observation_event(
                {
                    "event_type": "mcp_tool_call",
                    "call_id": call_id,
                    "client": self._client_name,
                    "tool_ref": tool_ref,
                    "input": input,
                }
            )
        )
        try:
            result = self._inner.call_tool(tool_ref, input)
        except Exception as exc:  # noqa: BLE001
            self._audit_logger.record(
                enrich_observation_event(
                    {
                        "event_type": "mcp_tool_error",
                        "call_id": call_id,
                        "client": self._client_name,
                        "tool_ref": tool_ref,
                        "error": str(exc),
                    }
                )
            )
            raise
        self._audit_logger.record(
            enrich_observation_event(
                {
                    "event_type": "mcp_tool_result",
                    "call_id": call_id,
                    "client": self._client_name,
                    "tool_ref": tool_ref,
                    "result": result,
                }
            )
        )
        return result

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _serialize_llm_request(request: LlmGenerateRequest) -> dict[str, Any]:
    return {
        "response_format": request.response_format,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "attachments": [
                    {
                        "path": attachment.path,
                        "name": attachment.name,
                        "mime_type": attachment.mime_type,
                    }
                    for attachment in message.attachments
                ],
            }
            for message in request.messages
        ],
    }
