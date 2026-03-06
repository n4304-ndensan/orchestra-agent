from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

LlmRole = Literal["system", "user", "assistant"]
LlmResponseFormat = Literal["text", "json_object"]


@dataclass(slots=True, frozen=True)
class LlmMessage:
    role: LlmRole
    content: str


@dataclass(slots=True, frozen=True)
class LlmGenerateRequest:
    messages: tuple[LlmMessage, ...]
    response_format: LlmResponseFormat = "text"
    temperature: float = 0.0
    max_tokens: int | None = None


class ILlmClient(Protocol):
    def generate(self, request: LlmGenerateRequest) -> str:
        ...

