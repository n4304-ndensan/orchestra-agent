from __future__ import annotations

import json
from typing import Any

import httpx

from orchestra_agent.adapters.llm import OpenAILlmClient
from orchestra_agent.ports import LlmGenerateRequest, LlmMessage


def test_openai_llm_client_sends_chat_completion_request() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"steps":[{"step_id":"calculate_totals",'
                                '"resolved_input":{"column":"D"}}]}'
                            )
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAILlmClient(
        api_key="test-key",
        model="gpt-4.1-mini",
        base_url="https://api.openai.com",
        transport=transport,
    )

    response = client.generate(
        LlmGenerateRequest(
            messages=(
                LlmMessage(role="system", content="You are planner"),
                LlmMessage(role="user", content="objective"),
            ),
            response_format="json_object",
            temperature=0.0,
            max_tokens=300,
        )
    )
    client.close()

    assert response.startswith("{")
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["auth"] == "Bearer test-key"
    assert captured["payload"]["model"] == "gpt-4.1-mini"
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_openai_llm_client_raises_on_missing_choices() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []}))
    client = OpenAILlmClient(
        api_key="test-key",
        model="gpt-4.1-mini",
        transport=transport,
    )
    try:
        try:
            client.generate(
                LlmGenerateRequest(messages=(LlmMessage(role="user", content="hello"),))
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected RuntimeError")
    finally:
        client.close()
