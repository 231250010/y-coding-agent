from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)


Message = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def as_message_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True, slots=True)
class AssistantResponse:
    content: str | None = None
    tool_calls: Sequence[ToolCall] = field(default_factory=tuple)
    finish_reason: str | None = None


class ModelError(RuntimeError):
    """A user-facing model invocation failure."""


class ChatModel(Protocol):
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AssistantResponse: ...


class OpenAIChatModel:
    """Thin transport adapter. It contains no agent orchestration logic."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        sleep: Any = time.sleep,
    ) -> None:
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self.model = model
        self.max_retries = max_retries
        self._sleep = sleep

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AssistantResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
        }
        if tools:
            request.update({"tools": list(tools), "tool_choice": "auto"})

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.chat.completions.create(**request)
                if not response.choices:
                    raise ModelError("模型返回了空 choices")
                choice = response.choices[0]
                message = choice.message
                calls = tuple(
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=call.function.arguments,
                    )
                    for call in (message.tool_calls or [])
                    if call.type == "function"
                )
                return AssistantResponse(
                    content=message.content,
                    tool_calls=calls,
                    finish_reason=choice.finish_reason,
                )
            except (AuthenticationError, BadRequestError) as exc:
                raise ModelError(f"模型请求不可重试: {exc}") from exc
            except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise ModelError(f"模型请求在重试后仍失败: {exc}") from exc
                self._sleep(2**attempt)
            except ModelError:
                raise
            except Exception as exc:
                raise ModelError(f"模型请求失败: {exc}") from exc

        raise ModelError("模型请求失败")

