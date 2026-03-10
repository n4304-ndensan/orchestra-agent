from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from orchestra_agent.ports.llm_client import (
    ILlmClient,
    LlmAttachment,
    LlmGenerateRequest,
    LlmMessage,
)


class OpenAILlmClient(ILlmClient):
    """
    OpenAI chat-completions adapter.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com",
        timeout_seconds: float = 60.0,
        verify: bool | str = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            verify=verify,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def generate(self, request: LlmGenerateRequest) -> str:
        payload = self._build_payload(request)
        try:
            response = self._client.post(f"{self._base_url}/v1/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"OpenAI request timed out for model '{self._model}'.") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                "OpenAI request failed with "
                f"HTTP {exc.response.status_code} for model '{self._model}'."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"OpenAI request failed for model '{self._model}'.") from exc
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"OpenAI response was not valid JSON for model '{self._model}'."
            ) from exc
        return self._extract_message_text(body)

    def close(self) -> None:
        self._client.close()

    def _build_payload(self, request: LlmGenerateRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._build_message(message) for message in request.messages],
            "temperature": request.temperature,
        }
        if request.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        return payload

    def _build_message(self, message: LlmMessage) -> dict[str, Any]:
        if not message.attachments:
            return {
                "role": message.role,
                "content": message.content,
            }
        if message.role == "system":
            raise ValueError(
                "OpenAI system messages do not support file attachments in this client."
            )
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"type": "text", "text": message.content})
        for attachment in message.attachments:
            parts.append(self._build_attachment_part(attachment))
        return {
            "role": message.role,
            "content": parts,
        }

    @staticmethod
    def _build_attachment_part(attachment: LlmAttachment) -> dict[str, Any]:
        file_path = Path(attachment.path).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"LLM attachment '{file_path}' was not found.")
        mime_type = attachment.mime_type or mimetypes.guess_type(file_path.name)[0]
        normalized_mime_type = mime_type or "application/octet-stream"
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
        file_name = attachment.name or file_path.name
        return {
            "type": "file",
            "file": {
                "filename": file_name,
                "file_data": f"data:{normalized_mime_type};base64,{encoded}",
            },
        }

    @staticmethod
    def _extract_message_text(body: Any) -> str:
        message = OpenAILlmClient._extract_choice_message(body)
        return OpenAILlmClient._extract_content_text(message.get("content"))

    @staticmethod
    def _extract_choice_message(body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise RuntimeError("OpenAI response must be an object.")
        if "error" in body:
            raise RuntimeError(f"OpenAI API error: {body['error']}")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenAI response did not contain choices.")

        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError("OpenAI response choice must be an object.")

        message = first.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("OpenAI response choice.message must be an object.")
        return message

    @staticmethod
    def _extract_content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            if parts:
                return "".join(parts)

        raise RuntimeError("OpenAI response did not contain textual message content.")
