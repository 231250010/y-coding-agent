from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .context import ContextManager
from .execution_state import ExecutionState
from .model import AssistantResponse, ChatModel, Message, ModelError, ToolCall
from .providers import ToolProvider
from .prompts import SUMMARY_PROMPT, SYSTEM_PROMPT
from .task_list import TaskListState
from .tools import ToolResult


EventCallback = Callable[[str, dict[str, Any]], None]
MAX_DECISION_SUMMARY_CHARS = 500


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
        model_call_lock: threading.RLock | None = None,
        execution_state: ExecutionState | None = None,
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
        self.model_call_lock = model_call_lock or threading.RLock()
        self.execution_state = execution_state or ExecutionState()
        self.history: list[Message] = [{"role": "system", "content": system_prompt}]

    def clear(self) -> None:
        self.task_list.replace("", [])
        self.execution_state = ExecutionState()
        self.history = [{"role": "system", "content": self.system_prompt}]

    def close(self) -> None:
        closer = getattr(self.tools, "close", None)
        if callable(closer):
            closer()

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
        self.execution_state.begin_run()
        self._sync_task_list_anchor()
        self.history.append({"role": "user", "content": task})
        self._checkpoint()
        last_error: str | None = None
        repeated_errors = 0
        completion_reminded = False

        for step in range(1, self.max_steps + 1):
            self._check_cancelled()
            self._sync_task_list_anchor()
            self.on_event("model_start", {"step": step, "max_steps": self.max_steps})
            compacted = self.context.compact(self.history, self._summarize)
            if compacted != self.history:
                self.history = compacted
                self._checkpoint()
            response = self._complete_model(step)
            self._check_cancelled()
            self._append_assistant(response)

            if response.tool_calls and (response.content or "").strip():
                self.on_event(
                    "decision_summary",
                    {
                        "content": (response.content or "").strip()[
                            :MAX_DECISION_SUMMARY_CHARS
                        ],
                        "step": step,
                        "tools": [call.name for call in response.tool_calls],
                    },
                )

            if not response.tool_calls:
                content = (response.content or "").strip()
                if not content:
                    self.execution_state.mark_failed()
                    raise AgentStopped("模型既未返回文本，也未调用工具")
                if (
                    self.execution_state.has_unreported_evidence_gap
                    and not completion_reminded
                    and step < self.max_steps
                ):
                    completion_reminded = True
                    self.history.append(
                        {
                            "role": "system",
                            "content": (
                                "完成门禁：当前修改版本尚无修改后的成功验证证据。"
                                "请运行相关测试、构建或静态检查；如果客观上无法验证，"
                                "下一次最终回答必须明确说明未验证原因，不能声称测试已经通过。"
                            ),
                        }
                    )
                    self._checkpoint()
                    continue
                # Report each new evidence gap once. A later unrelated user turn
                # should not inherit the same warning indefinitely.
                verified = not self.execution_state.has_unreported_evidence_gap
                self.execution_state.mark_completed(verified=verified)
                if not verified:
                    content = "⚠️ 当前修改尚未获得修改后的成功验证证据。\n\n" + content
                self.on_event(
                    "final",
                    {
                        "content": content,
                        "step": step,
                        "execution": self.execution_state.completion_evidence(),
                    },
                )
                self._checkpoint()
                return content

            for call, result in self._execute_calls(response.tool_calls):
                try:
                    observed_arguments = json.loads(call.arguments)
                except json.JSONDecodeError:
                    observed_arguments = {}
                if not isinstance(observed_arguments, dict):
                    observed_arguments = {}
                self.execution_state.observe(call.name, observed_arguments, result)
                self.history.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result.to_message()}
                )
                # Persist protocol history before emitting optional UI details so
                # a crash cannot leave disk changes without their ToolResult.
                self._checkpoint()
                self.on_event(
                    "tool_end",
                    {
                        "name": call.name,
                        "ok": result.ok,
                        "output": result.output,
                        "error": result.error,
                        "changes": result.changes.to_event(change_tracker.turn_changes)
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
                        self.execution_state.mark_failed()
                        raise AgentStopped(f"连续三次发生相同工具错误，已停止: {result.error}")

        self.execution_state.mark_failed()
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
            self.execution_state.mark_cancelled()
            raise AgentCancelled("任务已由用户停止")

    def _checkpoint(self) -> None:
        self.on_event(
            "checkpoint",
            {
                "history": [dict(message) for message in self.history],
                "execution_state": self.execution_state.to_storage(),
            },
        )

    def _append_assistant(self, response: AssistantResponse) -> None:
        message: Message = {"role": "assistant", "content": response.content}
        if response.reasoning_content is not None:
            # DeepSeek thinking requires every assistant reasoning payload to be
            # replayed unchanged whenever that message remains in history. This
            # also covers internal continuation such as the completion gate.
            message["reasoning_content"] = response.reasoning_content
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

    def _complete_model(self, step: int) -> AssistantResponse:
        with self.model_call_lock:
            complete_stream = getattr(self.model, "complete_stream", None)
            if not callable(complete_stream):
                return self.model.complete(self.history, self.tools.schemas())

            parts: list[str] = []

            def receive(delta: str) -> None:
                self._check_cancelled()
                if not delta:
                    return
                parts.append(delta)
                self.on_event(
                    "assistant_delta",
                    {"delta": delta, "content": "".join(parts), "step": step},
                )

            return complete_stream(self.history, self.tools.schemas(), receive)

    def _summarize(self, old_conversation: str) -> str:
        self.on_event("summary_start", {})
        with self.model_call_lock:
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
