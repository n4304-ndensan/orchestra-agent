from __future__ import annotations

from pathlib import Path

import pytest

from orchestra_agent.adapters.llm import ChatGptPlaywrightLlmClient
from orchestra_agent.ports import LlmAttachment, LlmGenerateRequest, LlmMessage


class FakeChatSession:
    def __init__(self, response: str = '{"ok":true}') -> None:
        self.response = response
        self.start_calls: list[str] = []
        self.chat_calls: list[tuple[str, list[str] | None]] = []
        self.closed = False

    def start_chat(self, url: str) -> None:
        self.start_calls.append(url)

    def chat(self, message: str, file_paths: list[str] | None = None) -> str:
        self.chat_calls.append((message, file_paths))
        return self.response

    def close(self) -> None:
        self.closed = True


def test_chatgpt_playwright_llm_client_forwards_prompt_and_files() -> None:
    captured: dict[str, object] = {}
    session = FakeChatSession()

    def factory(chrome_path: str, profile_dir: Path | None, port: int) -> FakeChatSession:
        captured["chrome_path"] = chrome_path
        captured["profile_dir"] = profile_dir
        captured["port"] = port
        return session

    client = ChatGptPlaywrightLlmClient(
        start_url="https://chatgpt.com/g/private-agent",
        chrome_path=r"C:\Chrome\chrome.exe",
        profile_dir=Path(".chatgpt-profile"),
        port=9333,
        session_factory=factory,
    )

    try:
        response = client.generate(
            LlmGenerateRequest(
                messages=(
                    LlmMessage(role="system", content="You are a file automation planner."),
                    LlmMessage(
                        role="user",
                        content="Read the first screenshot.",
                        attachments=(LlmAttachment(path="input/old.png"),),
                    ),
                    LlmMessage(role="assistant", content="I will inspect it."),
                    LlmMessage(
                        role="user",
                        content="Return JSON only.",
                        attachments=(LlmAttachment(path="input/latest.png"),),
                    ),
                ),
                response_format="json_object",
                max_tokens=400,
            )
        )
    finally:
        client.close()

    assert response == '{"ok":true}'
    assert captured == {
        "chrome_path": r"C:\Chrome\chrome.exe",
        "profile_dir": Path(".chatgpt-profile"),
        "port": 9333,
    }
    assert session.start_calls == ["https://chatgpt.com/g/private-agent"]
    assert session.chat_calls == [
        (
            "System instructions:\n"
            "You are a file automation planner.\n\n"
            "Conversation transcript:\n"
            "[USER]\n"
            "Read the first screenshot.\n\n"
            "[ASSISTANT]\n"
            "I will inspect it.\n\n"
            "[USER]\n"
            "Return JSON only.\n\n"
            "Output contract:\n"
            "Return ONLY a valid JSON object. Do not add markdown fences or commentary.\n\n"
            "Output budget:\n"
            "Keep the response concise enough to fit within roughly 400 tokens.",
            ["input/latest.png"],
        )
    ]
    assert session.closed is True


def test_chatgpt_playwright_llm_client_reuses_session_between_requests() -> None:
    created_sessions: list[FakeChatSession] = []

    def factory(chrome_path: str, profile_dir: Path | None, port: int) -> FakeChatSession:
        session = FakeChatSession(response="ok")
        created_sessions.append(session)
        return session

    client = ChatGptPlaywrightLlmClient(
        start_url="https://chatgpt.com/ja-JP/",
        session_factory=factory,
    )

    try:
        first = client.generate(
            LlmGenerateRequest(messages=(LlmMessage(role="user", content="first"),))
        )
        second = client.generate(
            LlmGenerateRequest(messages=(LlmMessage(role="user", content="second"),))
        )
    finally:
        client.close()

    assert first == "ok"
    assert second == "ok"
    assert len(created_sessions) == 1
    assert created_sessions[0].start_calls == ["https://chatgpt.com/ja-JP/"]
    assert len(created_sessions[0].chat_calls) == 2


def test_chatgpt_playwright_llm_client_wraps_session_errors() -> None:
    class BrokenChatSession(FakeChatSession):
        def chat(self, message: str, file_paths: list[str] | None = None) -> str:
            raise RuntimeError("browser disconnected")

    client = ChatGptPlaywrightLlmClient(
        start_url="https://chatgpt.com/g/private-agent",
        session_factory=lambda chrome_path, profile_dir, port: BrokenChatSession(),
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="ChatGPT Playwright request failed.*browser disconnected",
        ):
            client.generate(
                LlmGenerateRequest(messages=(LlmMessage(role="user", content="hello"),))
            )
    finally:
        client.close()


def test_chatgpt_playwright_llm_client_wraps_startup_errors_with_reason() -> None:
    client = ChatGptPlaywrightLlmClient(
        start_url="https://chatgpt.com/g/private-agent",
        session_factory=lambda chrome_path, profile_dir, port: (_ for _ in ()).throw(
            RuntimeError("CDPポートが応答しません")
        ),
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="ChatGPT Playwright session initialization failed.*CDPポートが応答しません",
        ):
            client.generate(
                LlmGenerateRequest(messages=(LlmMessage(role="user", content="hello"),))
            )
    finally:
        client.close()
