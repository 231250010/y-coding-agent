from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from coding_agent.agent import AgentStopped, CodingAgent
from coding_agent.changes import ConversationChangeTracker
from coding_agent.context import ContextManager
from coding_agent.model import AssistantResponse, Message, ToolCall
from coding_agent.tools import ToolRegistry, ToolResult


class ScriptedModel:
    def __init__(self, responses: Sequence[AssistantResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[Message], Sequence[dict[str, Any]] | None]] = []

    def complete(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] | None = None
    ) -> AssistantResponse:
        self.requests.append((list(messages), tools))
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)


def make_agent(tmp_path: Path, responses: Sequence[AssistantResponse], max_steps: int = 10) -> tuple[CodingAgent, ScriptedModel]:
    model = ScriptedModel(responses)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True)
    return CodingAgent(model, tools, ContextManager(100_000), max_steps=max_steps), model


def call(identifier: str, name: str, args: Any) -> ToolCall:
    raw = args if isinstance(args, str) else json.dumps(args)
    return ToolCall(identifier, name, raw)


def test_plain_answer(tmp_path: Path) -> None:
    agent, model = make_agent(tmp_path, [AssistantResponse("完成")])
    assert agent.run("做任务") == "完成"
    assert model.requests[0][0][-1] == {"role": "user", "content": "做任务"}


def test_agent_emits_incremental_assistant_text_when_model_supports_streaming(
    tmp_path: Path,
) -> None:
    class StreamingModel:
        def complete_stream(
            self,
            _messages: Sequence[Message],
            _tools: Sequence[dict[str, Any]],
            on_delta: Any,
        ) -> AssistantResponse:
            on_delta("第一段")
            on_delta("第二段")
            return AssistantResponse("第一段第二段")

        def complete(self, *_args: Any, **_kwargs: Any) -> AssistantResponse:
            raise AssertionError("main model call should stream")

    events: list[tuple[str, dict[str, Any]]] = []
    agent = CodingAgent(
        StreamingModel(),  # type: ignore[arg-type]
        ToolRegistry(tmp_path),
        ContextManager(100_000),
        on_event=lambda name, data: events.append((name, data)),
    )

    assert agent.run("回答") == "第一段第二段"
    deltas = [data for name, data in events if name == "assistant_delta"]
    assert [item["delta"] for item in deltas] == ["第一段", "第二段"]
    assert deltas[-1]["content"] == "第一段第二段"


def test_single_tool_round_trip(tmp_path: Path) -> None:
    responses = [
        AssistantResponse(tool_calls=[call("c1", "write_file", {"path": "a.txt", "content": "hello"})]),
        AssistantResponse("已写入"),
    ]
    agent, model = make_agent(tmp_path, responses)
    assert agent.run("写文件") == "已写入"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"
    second_request = model.requests[1][0]
    assert second_request[-1]["role"] == "tool"
    assert second_request[-1]["tool_call_id"] == "c1"


def test_tool_end_exposes_local_changes_without_sending_them_to_model(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    tracker = ConversationChangeTracker(tmp_path)
    model = ScriptedModel([
        AssistantResponse(tool_calls=[call("c1", "write_file", {"path": "a.txt", "content": "hello\n"})]),
        AssistantResponse("done"),
    ])
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    agent = CodingAgent(
        model,
        tools,
        ContextManager(100_000),
        on_event=lambda name, data: events.append((name, data)),
    )

    agent.run("write")

    event = next(data for name, data in events if name == "tool_end")
    assert event["changes"]["paths"] == ["a.txt"]
    tool_payload = json.loads(model.requests[1][0][-1]["content"])
    assert "changes" not in tool_payload



def test_multiple_tools_are_executed_in_order(tmp_path: Path) -> None:
    responses = [
        AssistantResponse(
            tool_calls=[
                call("c1", "write_file", {"path": "a.txt", "content": "old"}),
                call("c2", "replace_text", {"path": "a.txt", "old_text": "old", "new_text": "new"}),
            ]
        ),
        AssistantResponse("done"),
    ]
    agent, _ = make_agent(tmp_path, responses)
    assert agent.run("change") == "done"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new"


class ConcurrencyProbeProvider:
    def __init__(self, parallel_names: set[str]) -> None:
        self.parallel_names = parallel_names
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test probe",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("read_probe", "write_probe")
        ]

    def can_run_parallel(self, name: str, _arguments: dict[str, Any]) -> bool:
        return name in self.parallel_names

    def execute(self, name: str, _arguments: dict[str, Any]) -> ToolResult:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.08)
            return ToolResult(True, output=name)
        finally:
            with self.lock:
                self.active -= 1


def test_independent_read_only_tool_calls_run_in_parallel_and_keep_message_order() -> None:
    provider = ConcurrencyProbeProvider({"read_probe"})
    model = ScriptedModel(
        [
            AssistantResponse(
                tool_calls=[
                    call("c1", "read_probe", {}),
                    call("c2", "read_probe", {}),
                    call("c3", "read_probe", {}),
                ]
            ),
            AssistantResponse("done"),
        ]
    )
    agent = CodingAgent(
        model, provider, ContextManager(100_000), max_parallel_tools=2
    )

    assert agent.run("inspect") == "done"
    assert provider.max_active == 2
    tool_messages = [
        message for message in model.requests[1][0] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == ["c1", "c2", "c3"]


def test_mutating_tool_calls_remain_serial() -> None:
    provider = ConcurrencyProbeProvider({"read_probe"})
    model = ScriptedModel(
        [
            AssistantResponse(
                tool_calls=[
                    call("c1", "write_probe", {}),
                    call("c2", "write_probe", {}),
                ]
            ),
            AssistantResponse("done"),
        ]
    )
    agent = CodingAgent(model, provider, ContextManager(100_000))

    assert agent.run("mutate") == "done"
    assert provider.max_active == 1


def test_invalid_json_is_returned_to_model(tmp_path: Path) -> None:
    responses = [
        AssistantResponse(tool_calls=[call("c1", "read_file", "{bad")]),
        AssistantResponse("recovered"),
    ]
    agent, model = make_agent(tmp_path, responses)
    assert agent.run("read") == "recovered"
    payload = json.loads(model.requests[1][0][-1]["content"])
    assert payload["ok"] is False and "合法 JSON" in payload["error"]


def test_unknown_tool_is_returned_to_model(tmp_path: Path) -> None:
    responses = [AssistantResponse(tool_calls=[call("c1", "nope", {})]), AssistantResponse("ok")]
    agent, model = make_agent(tmp_path, responses)
    assert agent.run("task") == "ok"
    assert "未知工具" in model.requests[1][0][-1]["content"]


def test_three_identical_errors_stop_loop(tmp_path: Path) -> None:
    responses = [AssistantResponse(tool_calls=[call(f"c{i}", "read_file", {"path": "missing"})]) for i in range(3)]
    agent, _ = make_agent(tmp_path, responses)
    with pytest.raises(AgentStopped, match="连续三次"):
        agent.run("read")


def test_success_resets_repeated_error_counter(tmp_path: Path) -> None:
    responses = [
        AssistantResponse(tool_calls=[call("c1", "read_file", {"path": "missing"})]),
        AssistantResponse(tool_calls=[call("c2", "write_file", {"path": "a", "content": "ok"})]),
        AssistantResponse(tool_calls=[call("c3", "read_file", {"path": "missing"})]),
        AssistantResponse("done"),
    ]
    agent, _ = make_agent(tmp_path, responses)
    assert agent.run("task") == "done"


def test_max_steps_stops(tmp_path: Path) -> None:
    responses = [AssistantResponse(tool_calls=[call("c1", "list_files", {})])]
    agent, _ = make_agent(tmp_path, responses, max_steps=1)
    with pytest.raises(AgentStopped, match="最大步骤数"):
        agent.run("loop")


def test_clear_preserves_only_system_prompt(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path, [AssistantResponse("done")])
    agent.run("task")
    agent.clear()
    assert len(agent.history) == 1 and agent.history[0]["role"] == "system"


def test_cancel_stops_before_model_call(tmp_path: Path) -> None:
    model = ScriptedModel([AssistantResponse("should not be used")])
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True)
    agent = CodingAgent(
        model,
        tools,
        ContextManager(100_000),
        is_cancelled=lambda: True,
    )
    with pytest.raises(AgentStopped, match="用户停止"):
        agent.run("task")
    assert not model.requests


def test_agent_retry_workflow_keeps_final_cumulative_diff(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []
    responses = [
        AssistantResponse(tool_calls=[call(
            "c1",
            "write_file",
            {"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"},
        )]),
        AssistantResponse(tool_calls=[call(
            "c2",
            "run_command",
            {"command": 'python -c "import sys; sys.exit(1)"', "timeout_seconds": 10},
        )]),
        AssistantResponse(tool_calls=[call(
            "c3",
            "replace_text",
            {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"},
        )]),
        AssistantResponse(tool_calls=[call(
            "c4",
            "run_command",
            {"command": 'python -c "from calc import add; assert add(2, 3) == 5"', "timeout_seconds": 10},
        )]),
        AssistantResponse("修复完成"),
    ]
    model = ScriptedModel(responses)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    agent = CodingAgent(
        model,
        tools,
        ContextManager(100_000),
        on_event=lambda name, data: events.append((name, data)),
    )

    assert agent.run("实现并测试加法") == "修复完成"
    assert tracker.changes["calc.py"].segments[0].latest.text == "def add(a, b):\n    return a + b\n"
    assert any(
        data.get("changes", {}).get("paths") == ["calc.py"]
        for name, data in events
        if name == "tool_end"
    )
