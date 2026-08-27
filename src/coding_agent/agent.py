from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .context import ContextManager
from .model import AssistantResponse, ChatModel, Message, ModelError
from .prompts import SUMMARY_PROMPT, SYSTEM_PROMPT
from .tools import ToolRegistry, ToolResult


EventCallback = Callable[[str, dict[str, Any]], None]


class AgentStopped(RuntimeError):
    """Raised when the local agent loop reaches a safety termination condition."""


class CodingAgent:
    def __init__(
        self,
        model: ChatModel,
        tools: ToolRegistry,
        context: ContextManager,
        *,
        max_steps: int = 20,
        on_event: EventCallback | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.model = model
        self.tools = tools
        self.context = context
        self.max_steps = max_steps
        self.on_event = on_event or (lambda _name, _data: None)
        self.system_prompt = system_prompt
        self.history: list[Message] = [{"role": "system", "content": system_prompt}]

    def clear(self) -> None:
        self.history = [{"role": "system", "content": self.system_prompt}]

    def run(self, task: str) -> str:
        if not task.strip():
            raise ValueError("任务不能为空")
        self.history.append({"role": "user", "content": task})
        last_error: str | None = None
        repeated_errors = 0

        for step in range(1, self.max_steps + 1):
            self.on_event("model_start", {"step": step, "max_steps": self.max_steps})
            self.history = self.context.compact(self.history, self._summarize)
            response = self.model.complete(self.history, self.tools.schemas())
            self._append_assistant(response)

            if not response.tool_calls:
                content = (response.content or "").strip()
                if not content:
                    raise AgentStopped("模型既未返回文本，也未调用工具")
                self.on_event("final", {"content": content, "step": step})
                return content

            for call in response.tool_calls:
                self.on_event("tool_start", {"name": call.name, "arguments": call.arguments})
                result = self._execute_call(call.name, call.arguments)
                self.history.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result.to_message()}
                )
                self.on_event(
                    "tool_end",
                    {"name": call.name, "ok": result.ok, "output": result.output, "error": result.error},
                )
                if result.ok:
                    last_error = None
                    repeated_errors = 0
                else:
                    fingerprint = f"{call.name}:{result.error}"
                    repeated_errors = repeated_errors + 1 if fingerprint == last_error else 1
                    last_error = fingerprint
                    if repeated_errors >= 3:
                        raise AgentStopped(f"连续三次发生相同工具错误，已停止: {result.error}")

        raise AgentStopped(f"达到最大步骤数 {self.max_steps}，任务未正常结束")

    def _append_assistant(self, response: AssistantResponse) -> None:
        message: Message = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            message["tool_calls"] = [call.as_message_dict() for call in response.tool_calls]
        self.history.append(message)

    def _execute_call(self, name: str, raw_arguments: str) -> ToolResult:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return ToolResult(False, error=f"工具参数不是合法 JSON: {exc.msg}")
        if not isinstance(arguments, dict):
            return ToolResult(False, error="工具参数必须是 JSON 对象")
        return self.tools.execute(name, arguments)

    def _summarize(self, old_conversation: str) -> str:
        self.on_event("summary_start", {})
        response = self.model.complete(
            [
                {"role": "system", "content": "你负责忠实压缩编程任务上下文。"},
                {"role": "user", "content": SUMMARY_PROMPT.format(conversation=old_conversation)},
            ],
            None,
        )
        if response.tool_calls or not response.content:
            raise ModelError("上下文摘要响应无效")
        self.on_event("summary_end", {})
        return response.content

