from __future__ import annotations

import json
import sys
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


def test_tool_call_content_emits_bounded_decision_summary_before_tool(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    response = AssistantResponse(
        "目标：检查工作区。\n依据：用户要求先了解代码。\n下一步：列出文件。" + "补" * 600,
        tool_calls=[call("c1", "list_files", {})],
        reasoning_content="不可见的内部推理",
    )
    agent = CodingAgent(
        ScriptedModel([response, AssistantResponse("检查完成")]),
        ToolRegistry(tmp_path),
        ContextManager(100_000),
        on_event=lambda name, data: events.append((name, data)),
    )

    assert agent.run("检查") == "检查完成"
    names = [name for name, _data in events]
    assert names.index("decision_summary") < names.index("tool_start")
    decision = next(data for name, data in events if name == "decision_summary")
    assert len(decision["content"]) == 500
    assert decision["step"] == 1
    assert decision["tools"] == ["list_files"]
    assert "不可见的内部推理" not in json.dumps(decision, ensure_ascii=False)


def test_tool_call_without_visible_content_skips_decision_summary(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    agent = CodingAgent(
        ScriptedModel(
            [
                AssistantResponse(
                    tool_calls=[call("c1", "list_files", {})],
                    reasoning_content="只用于协议回传",
                ),
                AssistantResponse("完成"),
            ]
        ),
        ToolRegistry(tmp_path),
        ContextManager(100_000),
        on_event=lambda name, data: events.append((name, data)),
    )

    assert agent.run("检查") == "完成"
    assert all(name != "decision_summary" for name, _data in events)


def test_deepseek_reasoning_content_is_replayed_with_assistant_tool_call(
    tmp_path: Path,
) -> None:
    responses = [
        AssistantResponse(
            tool_calls=[call("c1", "read_file", {"path": "a.txt"})],
            finish_reason="tool_calls",
            reasoning_content="需要先读取目标文件",
        ),
        AssistantResponse("读取完成"),
    ]
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    agent, model = make_agent(tmp_path, responses)

    assert agent.run("读取文件") == "读取完成"
    assistant = model.requests[1][0][-2]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "需要先读取目标文件"
    assert assistant["tool_calls"][0]["id"] == "c1"


def test_final_reasoning_content_is_replayed_into_a_later_user_turn(
    tmp_path: Path,
) -> None:
    agent, model = make_agent(
        tmp_path,
        [
            AssistantResponse("第一轮完成", reasoning_content="第一轮内部分析"),
            AssistantResponse("第二轮完成"),
        ],
    )

    assert agent.run("第一轮") == "第一轮完成"
    assert agent.run("第二轮") == "第二轮完成"
    previous_assistant = next(
        message
        for message in model.requests[1][0]
        if message.get("role") == "assistant"
    )
    assert previous_assistant["reasoning_content"] == "第一轮内部分析"


def test_tool_end_exposes_local_changes_without_sending_them_to_model(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    tracker = ConversationChangeTracker(tmp_path)
    model = ScriptedModel([
        AssistantResponse(tool_calls=[call("c1", "write_file", {"path": "a.txt", "content": "hello\n"})]),
        AssistantResponse("done"),
        AssistantResponse("done without validation"),
    ])
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    agent = CodingAgent(
        model,
        tools,
        ContextManager(100_000),
        on_event=lambda name, data: events.append((name, data)),
    )

    result = agent.run("write")

    event = next(data for name, data in events if name == "tool_end")
    assert event["changes"]["paths"] == ["a.txt"]
    tool_payload = json.loads(model.requests[1][0][-1]["content"])
    assert "changes" not in tool_payload
    assert result.startswith("⚠️")
    assert agent.execution_state.outcome == "completed_unverified"
    assert any(
        checkpoint["history"][-1].get("role") == "tool"
        for name, checkpoint in events
        if name == "checkpoint" and checkpoint.get("history")
    )


def test_completion_gate_requests_validation_and_accepts_fresh_evidence(
    tmp_path: Path,
) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    model = ScriptedModel(
        [
            AssistantResponse(
                tool_calls=[call(
                    "c1",
                    "write_file",
                    {"path": "valid.py", "content": "answer = 42\n"},
                )]
            ),
            AssistantResponse("已经完成", reasoning_content="先总结当前修改"),
            AssistantResponse(
                tool_calls=[call(
                    "c2",
                    "run_process",
                    {
                        "argv": [sys.executable, "-m", "py_compile", "valid.py"],
                        "timeout_seconds": 10,
                    },
                )]
            ),
            AssistantResponse("已写入并通过语法检查"),
        ]
    )
    agent = CodingAgent(
        model,
        ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker),
        ContextManager(100_000),
    )

    assert agent.run("创建 Python 文件并验证") == "已写入并通过语法检查"
    assert model.requests[2][0][-1]["role"] == "system"
    assert "完成门禁" in str(model.requests[2][0][-1]["content"])
    assert model.requests[2][0][-2]["reasoning_content"] == "先总结当前修改"
    assert agent.execution_state.outcome == "completed"
    assert not agent.execution_state.needs_validation


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
    agent.execution_state.mutation_revision = 1
    agent.clear()
    assert len(agent.history) == 1 and agent.history[0]["role"] == "system"
    assert agent.execution_state.to_storage()["mutation_revision"] == 0


def test_unverified_gap_is_not_repeated_on_unrelated_later_turn(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    model = ScriptedModel(
        [
            AssistantResponse(
                tool_calls=[call(
                    "c1", "write_file", {"path": "a.txt", "content": "hello"}
                )]
            ),
            AssistantResponse("第一次完成"),
            AssistantResponse("无法验证"),
            AssistantResponse("这是后续说明"),
        ]
    )
    agent = CodingAgent(
        model,
        ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker),
        ContextManager(100_000),
    )

    assert agent.run("写文本").startswith("⚠️")
    assert agent.run("解释一下") == "这是后续说明"
    assert agent.execution_state.outcome == "completed"


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
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
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
            "run_process",
            {"argv": [sys.executable, "-m", "pytest", "-q"], "timeout_seconds": 10},
        )]),
        AssistantResponse(tool_calls=[call(
            "c3",
            "replace_text",
            {
                "path": "calc.py",
                "old_text": "return a - b",
                "new_text": "return a + b  # fixed",
            },
        )]),
        AssistantResponse(tool_calls=[call(
            "c4",
            "run_process",
            {"argv": [sys.executable, "-m", "pytest", "-q"], "timeout_seconds": 10},
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
    assert tracker.changes["calc.py"].segments[0].latest.text == (
        "def add(a, b):\n    return a + b  # fixed\n"
    )
    assert agent.execution_state.outcome == "completed"
    assert agent.execution_state.verified_revision == agent.execution_state.mutation_revision
    assert any(
        data.get("changes", {}).get("paths") == ["calc.py"]
        for name, data in events
        if name == "tool_end"
    )
