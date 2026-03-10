from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestra_agent.ports.llm_client import ILlmClient, LlmGenerateRequest

type ChatGptSessionFactory = Callable[[str, Path | None, int], Any]


class ChatGptPlaywrightLlmClient(ILlmClient):
    """
    Browser-backed private-use LLM adapter for ChatGPT custom GPT pages.
    """

    def __init__(
        self,
        *,
        start_url: str,
        chrome_path: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        profile_dir: Path | None = None,
        port: int = 9222,
        session_factory: ChatGptSessionFactory | None = None,
    ) -> None:
        self._start_url = start_url
        self._chrome_path = chrome_path
        self._profile_dir = profile_dir
        self._port = port
        self._session_factory = session_factory or _default_session_factory
        self._session: Any | None = None

    def generate(self, request: LlmGenerateRequest) -> str:
        session = self._ensure_session()
        file_paths = _latest_attachment_paths(request)
        prompt = _request_to_chat_prompt(request)
        try:
            response = session.chat(prompt, file_paths=file_paths)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"ChatGPT Playwright request failed for '{self._start_url}'."
            ) from exc
        if not isinstance(response, str):
            raise RuntimeError(
                "ChatGPT Playwright client returned a non-text response."
            )
        return response

    def close(self) -> None:
        if self._session is None:
            return
        close = getattr(self._session, "close", None)
        if callable(close):
            close()
        self._session = None

    def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            session = self._session_factory(
                self._chrome_path,
                self._profile_dir,
                self._port,
            )
            session.start_chat(self._start_url)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"ChatGPT Playwright session initialization failed for '{self._start_url}'."
            ) from exc
        self._session = session
        return session


def _default_session_factory(chrome_path: str, profile_dir: Path | None, port: int) -> Any:
    from chat_gpt_playwright import ChatGPTClient  # type: ignore[import-untyped]

    return ChatGPTClient(
        chrome_path=chrome_path,
        profile_dir=profile_dir,
        port=port,
    )


def _latest_attachment_paths(request: LlmGenerateRequest) -> list[str] | None:
    for message in reversed(request.messages):
        if message.attachments:
            return [attachment.path for attachment in message.attachments]
    return None


def _request_to_chat_prompt(request: LlmGenerateRequest) -> str:
    system_messages = [
        message.content.strip()
        for message in request.messages
        if message.role == "system" and message.content.strip()
    ]
    transcript_messages = [
        message
        for message in request.messages
        if message.role != "system" and message.content.strip()
    ]

    sections: list[str] = []
    if system_messages:
        sections.append("System instructions:\n" + "\n\n".join(system_messages))

    if transcript_messages:
        transcript_lines = [
            f"[{message.role.upper()}]\n{message.content.strip()}"
            for message in transcript_messages
        ]
        sections.append("Conversation transcript:\n" + "\n\n".join(transcript_lines))

    if request.response_format == "json_object":
        sections.append(
            "Output contract:\n"
            "Return ONLY a valid JSON object. Do not add markdown fences or commentary."
        )

    if request.max_tokens is not None:
        sections.append(
            "Output budget:\n"
            f"Keep the response concise enough to fit within roughly {request.max_tokens} tokens."
        )

    prompt = "\n\n".join(section for section in sections if section.strip()).strip()
    if prompt:
        return prompt
    return "Return the best possible response for the current request."
