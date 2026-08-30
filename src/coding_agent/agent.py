from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .context import ContextManager
from .model import AssistantResponse, ChatModel, Message, ModelError, ToolCall
from .providers import ToolProvider
from .prompts import SUMMARY_PROMPT, SYSTEM_PROMPT
from .task_list import TaskListState
from .tools import ToolResult


EventCallback = Callable[[str, dict[str, Any]], None]


class AgentStopped(RuntimeError):
    """Raised when the local agent loop reaches a safety termination condition."""


class AgentCancelled(AgentStopped):
    """Raised when a user requests cancellation from an interactive frontend."""


class CodingAgent:
    def __init__(
        self,
        model: ChatModel,
        tools: ToolProvider,
        context: ContextManager,
        *,
        max_steps: int = 20,
        on_event: EventCallback | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        task_list: TaskListState | None = None,
        max_parallel_tools: int = 4,
    ) -> None:
        if max_parallel_tools < 1:
            raise ValueError("max_parallel_tools 必须至少为 1")
        self.model = model
        self.tools = tools
        self.context = context
        self.max_steps = max_steps
        self.on_event = on_event or (lambda _name, _data: None)
        self.is_cancelled = is_cancelled or (lambda: False)
        self.system_prompt = system_prompt
        self.task_list = task_list or TaskListState()
        self.max_parallel_tools = max_parallel_tools
        self.history: list[Message] = [{"role": "system", "content": system_prompt}]

    def clear(self) -> None:
        self.task_list.replace("", [])
        self.history = [{"role": "system", "content": self.system_prompt}]

    def restore_history(self, messages: list[Message]) -> None:
        restored = [
            dict(message)
            for message in messages
            if not TaskListState.is_anchor(message)
        ]
        if not restored or restored[0].get("role") != "system":
            restored.insert(0, {"role": "system", "content": self.system_prompt})
        self.history = restored
        self._sync_task_list_anchor()

    def run(self, task: str) -> str:
        if not task.strip():
            raise ValueError("任务不能为空")
        self._sync_task_list_anchor()
        self.history.append({"role": "user", "content": task})
        last_error: str | None = None
        repeated_errors = 0

        for step in range(1, self.max_steps + 1):
            self._check_cancelled()
            self._sync_task_list_anchor()
            self.on_event("model_start", {"step": step, "max_steps": self.max_steps})
            self.history = self.context.compact(self.history, self._summarize)
            response = self.model.complete(self.history, self.tools.schemas())
            self._check_cancelled()
            self._append_assistant(response)

            if not response.tool_calls:
                content = (response.content or "").strip()
                if not content:
                    raise AgentStopped("模型既未返回文本，也未调用工具")
                self.on_event("final", {"content": content, "step": step})
                return content

            for call, result in self._execute_calls(response.tool_calls):
                self.history.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result.to_message()}
                )
                self.on_event(
                    "tool_end",
                    {
                        "name": call.name,
                        "ok": result.ok,
                        "output": result.output,
                        "error": result.error,
                        "changes": result.changes.to_event(change_tracker.changes)
                        if (change_tracker := getattr(self.tools, "change_tracker", None))
                        else {"paths": [], "warning": None, "files": []},
                    },
                )
                self._check_cancelled()
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

    def _sync_task_list_anchor(self) -> None:
        history = [
            message for message in self.history if not TaskListState.is_anchor(message)
        ]
        snapshot = self.task_list.snapshot()
        if snapshot["objective"] or snapshot["items"]:
            insertion = 1 if history and history[0].get("role") == "system" else 0
            history.insert(insertion, self.task_list.system_message())
        self.history = history

    def _check_cancelled(self) -> None:
        if self.is_cancelled():
            raise AgentCancelled("任务已由用户停止")

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

    def _execute_calls(
        self, calls: Sequence[ToolCall]
    ) -> Iterator[tuple[ToolCall, ToolResult]]:
        ordered = list(calls)
        index = 0
        while index < len(ordered):
            self._check_cancelled()
            if not self._can_run_parallel(ordered[index]):
                call = ordered[index]
                self.on_event(
                    "tool_start", {"name": call.name, "arguments": call.arguments}
                )
                yield call, self._execute_call(call.name, call.arguments)
                index += 1
                continue

            end = index + 1
            while end < len(ordered) and self._can_run_parallel(ordered[end]):
                end += 1
            group = ordered[index:end]
            if len(group) == 1:
                call = group[0]
                self.on_event(
                    "tool_start", {"name": call.name, "arguments": call.arguments}
                )
                yield call, self._execute_call(call.name, call.arguments)
            else:
                for call in group:
                    self.on_event(
                        "tool_start", {"name": call.name, "arguments": call.arguments}
                    )
                with ThreadPoolExecutor(
                    max_workers=min(self.max_parallel_tools, len(group)),
                    thread_name_prefix="coding-agent-tool",
                ) as executor:
                    futures = [
                        executor.submit(self._execute_call, call.name, call.arguments)
                        for call in group
                    ]
                    group_results = [future.result() for future in futures]
                for call, result in zip(group, group_results):
                    yield call, result
            index = end

    def _can_run_parallel(self, call: ToolCall) -> bool:
        try:
            arguments = json.loads(call.arguments)
        except json.JSONDecodeError:
            return False
        if not isinstance(arguments, dict):
            return False
        checker = getattr(self.tools, "can_run_parallel", None)
        return bool(checker and checker(call.name, arguments))

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
