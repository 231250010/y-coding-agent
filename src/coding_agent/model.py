from __future__ import annotations

import time
from collections.abc import Callable
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
StreamCallback = Callable[[str], None]


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
    reasoning_content: str | None = None
    reasoning_content_present: bool = False

    def __post_init__(self) -> None:
        if self.reasoning_content is not None and not self.reasoning_content_present:
            object.__setattr__(self, "reasoning_content_present", True)


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
        return self._complete(messages, tools, max_tokens=None)

    def complete_with_max_tokens(
        self,
        messages: Sequence[Message],
        max_tokens: int,
    ) -> AssistantResponse:
        """Make an isolated, tool-free request with a hard output-token limit."""
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValueError("max_tokens 必须是正整数")
        return self._complete(messages, None, max_tokens=max_tokens)

    def _complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None,
        *,
        max_tokens: int | None,
    ) -> AssistantResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
        }
        if tools:
            request.update({"tools": list(tools), "tool_choice": "auto"})
        if max_tokens is not None:
            request["max_tokens"] = max_tokens

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
                reasoning_present, reasoning_content = self._optional_string_field(
                    message, "reasoning_content"
                )
                return AssistantResponse(
                    content=message.content,
                    tool_calls=calls,
                    finish_reason=choice.finish_reason,
                    reasoning_content=reasoning_content,
                    reasoning_content_present=reasoning_present,
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

    def complete_stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None,
        on_delta: StreamCallback,
    ) -> AssistantResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
        }
        if tools:
            request.update({"tools": list(tools), "tool_choice": "auto"})

        for attempt in range(self.max_retries + 1):
            received_chunk = False
            received_choice = False
            stream: Any = None
            try:
                stream = self._client.chat.completions.create(**request)
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                reasoning_content_present = False
                tool_parts: dict[int, dict[str, str]] = {}
                finish_reason: str | None = None
                for chunk in stream:
                    received_chunk = True
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        self._emit_delta(on_delta, "")
                        continue
                    received_choice = True
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    content = getattr(delta, "content", None) if delta else None
                    if isinstance(content, str) and content:
                        content_parts.append(content)
                    reasoning_present, reasoning = self._optional_string_field(
                        delta, "reasoning_content"
                    )
                    reasoning_content_present = (
                        reasoning_content_present or reasoning_present
                    )
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    self._emit_delta(on_delta, content if isinstance(content, str) else "")
                    for item in (getattr(delta, "tool_calls", None) or []):
                        index = int(getattr(item, "index", 0) or 0)
                        part = tool_parts.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        identifier = getattr(item, "id", None)
                        if isinstance(identifier, str) and identifier:
                            part["id"] = identifier
                        function = getattr(item, "function", None)
                        name = getattr(function, "name", None) if function else None
                        arguments = (
                            getattr(function, "arguments", None) if function else None
                        )
                        if isinstance(name, str):
                            part["name"] += name
                        if isinstance(arguments, str):
                            part["arguments"] += arguments
                    candidate_reason = getattr(choice, "finish_reason", None)
                    if candidate_reason is not None:
                        finish_reason = str(candidate_reason)

                if not received_choice:
                    raise ModelError("模型返回了空流式响应")
                calls: list[ToolCall] = []
                for index in sorted(tool_parts):
                    part = tool_parts[index]
                    if not part["id"] or not part["name"]:
                        raise ModelError("模型返回了不完整的流式工具调用")
                    calls.append(
                        ToolCall(part["id"], part["name"], part["arguments"])
                    )
                return AssistantResponse(
                    content="".join(content_parts) or None,
                    tool_calls=tuple(calls),
                    finish_reason=finish_reason,
                    reasoning_content="".join(reasoning_parts) or None,
                    reasoning_content_present=reasoning_content_present,
                )
            except _StreamCallbackAbort as exc:
                raise exc.original
            except (AuthenticationError, BadRequestError) as exc:
                raise ModelError(f"模型请求不可重试: {exc}") from exc
            except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
                if received_chunk or attempt >= self.max_retries:
                    detail = "模型流式响应中断" if received_chunk else "模型请求在重试后仍失败"
                    raise ModelError(f"{detail}: {exc}") from exc
                self._sleep(2**attempt)
            except ModelError:
                raise
            except Exception as exc:
                if received_chunk:
                    raise ModelError(f"模型流式响应中断: {exc}") from exc
                if attempt >= self.max_retries:
                    raise ModelError(f"模型请求失败: {exc}") from exc
                self._sleep(2**attempt)
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

        raise ModelError("模型流式请求未返回结果")

    @staticmethod
    def _emit_delta(callback: StreamCallback, content: str) -> None:
        try:
            callback(content)
        except Exception as exc:
            raise _StreamCallbackAbort(exc) from exc

    @staticmethod
    def _optional_string_field(value: Any, name: str) -> tuple[bool, str | None]:
        """Return extension-field presence and value without SDK coupling."""
        if value is None:
            return False, None
        if isinstance(value, dict):
            present = name in value
            candidate = value.get(name)
        else:
            model_extra = getattr(value, "model_extra", None)
            fields_set = getattr(value, "model_fields_set", None)
            has_sdk_metadata = isinstance(model_extra, dict) or isinstance(
                fields_set, (set, frozenset)
            )
            present = bool(
                (isinstance(model_extra, dict) and name in model_extra)
                or (isinstance(fields_set, (set, frozenset)) and name in fields_set)
            )
            if not has_sdk_metadata:
                present = hasattr(value, name)
            if isinstance(model_extra, dict) and name in model_extra:
                candidate = model_extra.get(name)
            else:
                candidate = getattr(value, name, None)
        return present, candidate if isinstance(candidate, str) else None


class _StreamCallbackAbort(Exception):
    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original
