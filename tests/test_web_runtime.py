from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from coding_agent.local_settings import LocalSettings
from coding_agent.model import AssistantResponse, Message, ToolCall
from coding_agent.web_runtime import RuntimeConflict, RuntimeNotFound, WebRuntime


class ScriptedModel:
    def __init__(self, responses: Sequence[AssistantResponse]) -> None:
        self.responses = list(responses)

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AssistantResponse:
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)


def settings(root: Path) -> LocalSettings:
    return LocalSettings(
        api_key="test-key",
        model="test-model",
        base_url="https://example.invalid",
        workspace=str(root),
    )


def wait_until_idle(runtime: WebRuntime, task_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = next(item for item in runtime.snapshot()["tasks"] if item["id"] == task_id)
        if not task["running"]:
            return task
        time.sleep(0.01)
    raise AssertionError("task did not finish")


def wait_for_approval(runtime: WebRuntime) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        approvals = runtime.snapshot()["approvals"]
        if approvals:
            return approvals[0]
        time.sleep(0.01)
    raise AssertionError("approval did not appear")


def test_project_and_conversation_are_created_for_existing_directory(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))

    project = runtime.add_project(str(tmp_path))
    task = runtime.new_conversation(project["id"])
    state = runtime.snapshot()

    assert state["projects"] == [{"id": project["id"], "title": tmp_path.name, "path": str(tmp_path.resolve())}]
    assert state["tasks"][0]["project_id"] == project["id"]
    assert state["tasks"][0]["entries"] == []
    assert state["settings"]["api_key_configured"] is True
    assert "test-key" not in json.dumps(state)
    assert "api_key" not in state["settings"]


def test_project_rejects_missing_directory(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))

    with pytest.raises(ValueError, match="工作目录不存在"):
        runtime.add_project(str(tmp_path / "missing"))


def test_message_runs_existing_agent_loop_and_attaches_changes_once(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            AssistantResponse(
                tool_calls=[
                    ToolCall(
                        "call-1",
                        "write_file",
                        json.dumps({"path": "hello.txt", "content": "hello\n"}),
                    )
                ]
            ),
            AssistantResponse("文件已经写好。"),
        ]
    )
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: model)
    project = runtime.add_project(str(tmp_path))
    task = runtime.new_conversation(project["id"])

    runtime.send_message(task["id"], "写一个问候文件")
    completed = wait_until_idle(runtime, task["id"])

    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert [entry["kind"] for entry in completed["entries"]] == ["user", "tool", "assistant"]
    assert completed["entries"][-1]["change_paths"] == ["hello.txt"]
    assert sum(bool(entry["change_paths"]) for entry in completed["entries"]) == 1


def test_running_conversation_rejects_second_message(tmp_path: Path) -> None:
    class BlockingModel:
        def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] | None = None) -> AssistantResponse:
            time.sleep(0.2)
            return AssistantResponse("done")

    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=BlockingModel)
    task = runtime.new_conversation()

    runtime.send_message(task["id"], "first")
    with pytest.raises(RuntimeConflict, match="正在运行"):
        runtime.send_message(task["id"], "second")
    runtime.cancel(task["id"])
    wait_until_idle(runtime, task["id"])


def test_diff_is_scoped_to_the_requested_conversation(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            AssistantResponse(
                tool_calls=[
                    ToolCall(
                        "c1",
                        "write_file",
                        json.dumps({"path": "a.py", "content": "new\n"}),
                    )
                ]
            ),
            AssistantResponse("done"),
        ]
    )
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: model)
    project = runtime.add_project(str(tmp_path))
    changed = runtime.new_conversation(project["id"])
    untouched = runtime.new_conversation(project["id"])
    runtime.send_message(changed["id"], "change")
    wait_until_idle(runtime, changed["id"])

    diff = runtime.diff(changed["id"], "a.py")

    assert diff["path"] == "a.py"
    assert diff["added"] == 1
    assert any(row["kind"] == "added" and row["text"] == "new" for row in diff["rows"])
    with pytest.raises(RuntimeNotFound):
        runtime.diff(untouched["id"], "a.py")


def test_snapshot_never_exposes_api_key_when_loaded_from_local_settings(tmp_path: Path) -> None:
    secret = "local-secret-value"
    local = settings(tmp_path)
    local.api_key = secret
    runtime = WebRuntime(local, tmp_path, model_factory=lambda: ScriptedModel([]))

    assert secret not in json.dumps(runtime.snapshot(), ensure_ascii=False)


def test_review_command_waits_for_browser_decision_and_continues_after_rejection(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            AssistantResponse(
                tool_calls=[
                    ToolCall(
                        "install-1",
                        "run_command",
                        json.dumps({"command": "python -m pip install example-package"}),
                    )
                ]
            ),
            AssistantResponse("已尊重拒绝决定。"),
        ]
    )
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: model)
    project = runtime.add_project(str(tmp_path))
    task = runtime.new_conversation(project["id"])

    runtime.send_message(task["id"], "安装依赖")
    approval = wait_for_approval(runtime)
    runtime.resolve_approval(approval["id"], False)
    completed = wait_until_idle(runtime, task["id"])

    assert approval["task_id"] == task["id"]
    assert "pip install" in approval["command"]
    assert completed["entries"][-1]["text"] == "已尊重拒绝决定。"
    assert any("用户未批准命令" in entry["text"] for entry in completed["entries"] if entry["kind"] == "error")
