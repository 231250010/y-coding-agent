from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from coding_agent.agent import AgentStopped, CodingAgent
from coding_agent.context import ContextManager
from coding_agent.model import AssistantResponse, Message, ToolCall
from coding_agent.tools import ToolRegistry


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
