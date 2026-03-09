from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from orchestra_agent.adapters.llm import OpenAILlmClient
from orchestra_agent.ports import LlmAttachment, LlmGenerateRequest, LlmMessage


def test_openai_llm_client_sends_chat_completion_request() -> None:
    captured: dict[str, Any] = {}
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    attachment = base / "notes.txt"
    attachment.write_text("Use this file.", encoding="utf-8")

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

    try:
        response = client.generate(
            LlmGenerateRequest(
                messages=(
                    LlmMessage(role="system", content="You are planner"),
                    LlmMessage(
                        role="user",
                        content="objective",
                        attachments=(LlmAttachment(path=str(attachment)),),
                    ),
                ),
                response_format="json_object",
                temperature=0.0,
                max_tokens=300,
            )
        )
        assert response.startswith("{")
        assert captured["url"].endswith("/v1/chat/completions")
        assert captured["auth"] == "Bearer test-key"
        assert captured["payload"]["model"] == "gpt-4.1-mini"
        assert captured["payload"]["response_format"] == {"type": "json_object"}
        assert captured["payload"]["messages"][1]["content"][1]["type"] == "file"
        assert captured["payload"]["messages"][1]["content"][1]["file"]["filename"] == "notes.txt"
    finally:
        client.close()
        shutil.rmtree(base, ignore_errors=True)


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
