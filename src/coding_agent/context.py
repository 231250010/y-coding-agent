from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from typing import Any

from .model import Message


Summarizer = Callable[[str], str]
SUMMARY_PREFIX = "较早会话摘要：\n"
SUMMARY_FALLBACK = "较早会话因上下文预算已被裁剪；请依据当前工作区和近期消息继续。"
SUMMARY_TOOL_RESULT_LIMIT = 4_000
HARD_TOOL_RESULT_MIN_CHARS = 512


class ContextManager:
    """Maintains a bounded chat history without splitting tool-call turns."""

    def __init__(self, max_tokens: int, *, trigger_ratio: float = 0.8, keep_recent_turns: int = 2) -> None:
        self.max_tokens = max_tokens
        self.trigger_ratio = trigger_ratio
        self.keep_recent_turns = keep_recent_turns

    @staticmethod
    def estimate_tokens(messages: Sequence[Message]) -> int:
        serialized = json.dumps(list(messages), ensure_ascii=False, separators=(",", ":"))
        # UTF-8 bytes / 4 is intentionally simple and model-independent. Character
        # count provides a more conservative floor for CJK-heavy conversations.
        return max(math.ceil(len(serialized.encode("utf-8")) / 4), math.ceil(len(serialized) / 2))

    def compact(self, messages: Sequence[Message], summarize: Summarizer) -> list[Message]:
        current = [dict(message) for message in messages]
        if self.estimate_tokens(current) <= self.max_tokens * self.trigger_ratio:
            return current

        prelude, turns = self._partition_turns(current)
        previous_notes = [message for message in prelude if self._is_summary_note(message)]
        stable_prelude = [message for message in prelude if not self._is_summary_note(message)]
        if len(turns) > self.keep_recent_turns:
            old_turns = turns[: -self.keep_recent_turns]
            recent_turns = turns[-self.keep_recent_turns :]
            old_messages = [message for turn in old_turns for message in turn]
            old_text = json.dumps(
                {
                    "previous_summaries": [
                        self._summary_body(message) for message in previous_notes
                    ],
                    "newly_compacted_turns": self._prepare_summary_messages(old_messages),
                },
                ensure_ascii=False,
                indent=2,
            )
            try:
                summary = summarize(old_text).strip()
                if not summary:
                    raise ValueError("empty summary")
                note = {"role": "system", "content": f"{SUMMARY_PREFIX}{summary}"}
            except Exception:
                note = {"role": "system", "content": SUMMARY_FALLBACK}
            current = stable_prelude + [note] + [
                message for turn in recent_turns for message in turn
            ]

        return self._truncate_large_results(current)

    @staticmethod
    def _is_summary_note(message: Message) -> bool:
        content = message.get("content")
        return message.get("role") == "system" and isinstance(content, str) and (
            content.startswith(SUMMARY_PREFIX) or content == SUMMARY_FALLBACK
        )

    @staticmethod
    def _summary_body(message: Message) -> str:
        content = str(message.get("content") or "")
        if content.startswith(SUMMARY_PREFIX):
            return content[len(SUMMARY_PREFIX) :]
        return content

    @staticmethod
    def _partition_turns(messages: list[Message]) -> tuple[list[Message], list[list[Message]]]:
        prelude: list[Message] = []
        turns: list[list[Message]] = []
        current: list[Message] | None = None
        for message in messages:
            if message.get("role") == "user":
                current = [message]
                turns.append(current)
            elif current is None:
                prelude.append(message)
            else:
                current.append(message)
        return prelude, turns

    def _truncate_large_results(self, messages: list[Message]) -> list[Message]:
        """Shrink recent tool results only when they exceed the hard context limit."""
        if self.estimate_tokens(messages) <= self.max_tokens:
            return messages

        result = [dict(message) for message in messages]
        candidates = [
            index
            for index, message in enumerate(result)
            if message.get("role") == "tool"
            and isinstance(message.get("content"), str)
            and len(str(message["content"])) > HARD_TOOL_RESULT_MIN_CHARS
        ]
        for index in candidates:
            if self.estimate_tokens(result) <= self.max_tokens:
                break
            content = str(result[index]["content"])
            minimum = min(HARD_TOOL_RESULT_MIN_CHARS, len(content) - 1)
            result[index]["content"] = self._head_tail(
                content,
                minimum,
                "近期工具输出因硬性上下文上限已截断",
            )
            if self.estimate_tokens(result) > self.max_tokens:
                continue

            low = minimum
            high = len(content) - 1
            best = minimum
            while low <= high:
                keep_chars = (low + high) // 2
                result[index]["content"] = self._head_tail(
                    content,
                    keep_chars,
                    "近期工具输出因硬性上下文上限已截断",
                )
                if self.estimate_tokens(result) <= self.max_tokens:
                    best = keep_chars
                    low = keep_chars + 1
                else:
                    high = keep_chars - 1
            result[index]["content"] = self._head_tail(
                content,
                best,
                "近期工具输出因硬性上下文上限已截断",
            )
        return result

    @classmethod
    def _prepare_summary_messages(cls, messages: list[Message]) -> list[Message]:
        """Bound old tool output before it is sent to the rolling summarizer."""
        prepared: list[Message] = []
        for message in messages:
            copied = dict(message)
            content = copied.get("content")
            if (
                copied.get("role") == "tool"
                and isinstance(content, str)
                and len(content) > SUMMARY_TOOL_RESULT_LIMIT
            ):
                copied["content"] = cls._head_tail(
                    content,
                    SUMMARY_TOOL_RESULT_LIMIT,
                    "旧工具输出为生成摘要已压缩",
                )
            prepared.append(copied)
        return prepared

    @staticmethod
    def _head_tail(content: str, keep_chars: int, reason: str) -> str:
        if len(content) <= keep_chars:
            return content
        head_size = (keep_chars + 1) // 2
        tail_size = keep_chars // 2
        omitted = len(content) - head_size - tail_size
        return (
            content[:head_size]
            + f"\n... [{reason}，省略 {omitted} 字符] ...\n"
            + content[-tail_size:]
        )
