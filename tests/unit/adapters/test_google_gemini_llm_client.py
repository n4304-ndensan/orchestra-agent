from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from orchestra_agent.adapters.llm import GoogleGeminiLlmClient
from orchestra_agent.ports import LlmAttachment, LlmGenerateRequest, LlmMessage


def test_google_gemini_llm_client_sends_generate_content_request() -> None:
    captured: dict[str, Any] = {}
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    attachment = base / "notes.txt"
    attachment.write_text("Use this file.", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("x-goog-api-key")
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"steps":[{"step_id":"calculate_totals",'
                                        '"resolved_input":{"column":"D"}}]}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = GoogleGeminiLlmClient(
        api_key="test-key",
        model="gemini-2.5-flash",
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
                    LlmMessage(role="assistant", content="draft"),
                ),
                response_format="json_object",
                temperature=0.1,
                max_tokens=300,
            )
        )
        assert response.startswith("{")
        assert captured["url"].endswith("/v1beta/models/gemini-2.5-flash:generateContent")
        assert captured["api_key"] == "test-key"
        assert captured["payload"]["systemInstruction"] == {"parts": [{"text": "You are planner"}]}
        assert captured["payload"]["contents"][0]["role"] == "user"
        assert captured["payload"]["contents"][0]["parts"][0] == {"text": "objective"}
        assert "inlineData" in captured["payload"]["contents"][0]["parts"][1]
        assert captured["payload"]["contents"][1] == {
            "role": "model",
            "parts": [{"text": "draft"}],
        }
        assert captured["payload"]["generationConfig"] == {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 300,
        }
    finally:
        client.close()
        shutil.rmtree(base, ignore_errors=True)


def test_google_gemini_llm_client_raises_on_missing_candidates() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"candidates": []}))
    client = GoogleGeminiLlmClient(
        api_key="test-key",
        model="gemini-2.5-flash",
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
