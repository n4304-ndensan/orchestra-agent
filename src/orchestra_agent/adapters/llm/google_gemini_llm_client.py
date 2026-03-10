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


class GoogleGeminiLlmClient(ILlmClient):
    """
    Google Gemini Developer API adapter backed by generateContent.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com",
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
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
        )

    def generate(self, request: LlmGenerateRequest) -> str:
        payload = self._build_payload(request)
        try:
            response = self._client.post(
                f"{self._base_url}/v1beta/models/{self._model}:generateContent",
                json=payload,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Google Gemini request timed out for model '{self._model}'."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                "Google Gemini request failed with "
                f"HTTP {exc.response.status_code} for model '{self._model}'."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Google Gemini request failed for model '{self._model}'."
            ) from exc
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Google Gemini response was not valid JSON for model '{self._model}'."
            ) from exc
        return self._extract_text(body)

    def close(self) -> None:
        self._client.close()

    def _build_payload(self, request: LlmGenerateRequest) -> dict[str, Any]:
        system_instruction, contents = self._split_messages(request.messages)
        if not contents:
            raise ValueError(
                "Google Gemini request requires at least one user or assistant message."
            )

        generation_config: dict[str, Any] = {
            "temperature": request.temperature,
            "responseMimeType": "application/json"
            if request.response_format == "json_object"
            else "text/plain",
        }
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_instruction is not None:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}],
            }
        return payload

    @staticmethod
    def _split_messages(
        messages: tuple[LlmMessage, ...],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                if message.attachments:
                    raise ValueError(
                        "Google Gemini system messages do not support file attachments "
                        "in this client."
                    )
                system_parts.append(message.content)
                continue

            contents.append(
                {
                    "role": GoogleGeminiLlmClient._to_google_role(message.role),
                    "parts": GoogleGeminiLlmClient._build_parts(message),
                }
            )

        system_instruction = "\n\n".join(part for part in system_parts if part.strip())
        return system_instruction or None, contents

    @staticmethod
    def _to_google_role(role: str) -> str:
        if role == "assistant":
            return "model"
        return "user"

    @staticmethod
    def _build_parts(message: LlmMessage) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"text": message.content})
        for attachment in message.attachments:
            parts.append(GoogleGeminiLlmClient._build_attachment_part(attachment))
        return parts

    @staticmethod
    def _build_attachment_part(attachment: LlmAttachment) -> dict[str, Any]:
        file_path = Path(attachment.path).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"LLM attachment '{file_path}' was not found.")
        mime_type = attachment.mime_type or mimetypes.guess_type(file_path.name)[0]
        normalized_mime_type = mime_type or "application/octet-stream"
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
        return {
            "inlineData": {
                "mimeType": normalized_mime_type,
                "data": encoded,
            }
        }

    @staticmethod
    def _extract_text(body: Any) -> str:
        if not isinstance(body, dict):
            raise RuntimeError("Google Gemini response must be an object.")
        if "error" in body:
            raise RuntimeError(f"Google Gemini API error: {body['error']}")

        first = GoogleGeminiLlmClient._extract_first_candidate(body)
        if not isinstance(first, dict):
            raise RuntimeError("Google Gemini candidate must be an object.")

        parts = GoogleGeminiLlmClient._extract_content_parts(first)
        text_parts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)

        if not text_parts:
            raise RuntimeError("Google Gemini response did not contain textual content.")
        return "".join(text_parts)

    @staticmethod
    def _extract_first_candidate(body: dict[str, Any]) -> Any:
        candidates = body.get("candidates")
        if isinstance(candidates, list) and candidates:
            return candidates[0]

        prompt_feedback = body.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            raise RuntimeError(f"Google Gemini response blocked: {prompt_feedback}")
        raise RuntimeError("Google Gemini response did not contain candidates.")

    @staticmethod
    def _extract_content_parts(candidate: dict[str, Any]) -> list[Any]:
        content = candidate.get("content")
        if not isinstance(content, dict):
            raise RuntimeError("Google Gemini candidate.content must be an object.")

        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise RuntimeError("Google Gemini candidate.content.parts must be a non-empty list.")
        return parts
