from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from coding_agent.local_settings import LocalSettings
from coding_agent.model import AssistantResponse, Message, ToolCall
from coding_agent.web_runtime import ChatEntry, RuntimeConflict, RuntimeNotFound, WebRuntime


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
    agent_root = tmp_path / "agent-root"
    workspace = tmp_path / "selected-workspace"
    agent_root.mkdir()
    workspace.mkdir()
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
    runtime = WebRuntime(settings(agent_root), agent_root, model_factory=lambda: model)
    project = runtime.add_project(str(workspace))
    task = runtime.new_conversation(project["id"])

    runtime.send_message(task["id"], "写一个问候文件")
    completed = wait_until_idle(runtime, task["id"])

    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (agent_root / "hello.txt").exists()
    assert [entry["kind"] for entry in completed["entries"]] == ["user", "tool", "assistant"]
    assert completed["entries"][-1]["change_paths"] == ["hello.txt"]
    assert sum(bool(entry["change_paths"]) for entry in completed["entries"]) == 1


def test_net_zero_temporary_file_is_removed_from_pending_changes(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    project = runtime.add_project(str(tmp_path))
    task_payload = runtime.new_conversation(project["id"])
    task = next(item for item in runtime.tasks if item.id == task_payload["id"])
    tracker = task.change_tracker

    before_create = tracker.capture_paths(["temporary-check.js"])
    (tmp_path / "temporary-check.js").write_text("check", encoding="utf-8")
    created = tracker.finish(before_create)
    runtime._handle_agent_event(
        task.id,
        "tool_end",
        {"name": "write_file", "ok": True, "changes": created.to_event(tracker.changes)},
    )

    before_delete = tracker.capture_paths(["temporary-check.js"])
    (tmp_path / "temporary-check.js").unlink()
    deleted = tracker.finish(before_delete)
    runtime._handle_agent_event(
        task.id,
        "tool_end",
        {"name": "run_command", "ok": True, "changes": deleted.to_event(tracker.changes)},
    )

    assert task.pending_change_paths == []


def test_restored_entries_hide_paths_without_a_remaining_diff(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    project = runtime.add_project(str(tmp_path))
    task_payload = runtime.new_conversation(project["id"])
    task = next(item for item in runtime.tasks if item.id == task_payload["id"])
    task.entries.append(ChatEntry("assistant", "done", ("temporary-check.js",)))
    runtime._save()

    restored = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    restored_task = next(item for item in restored.snapshot()["tasks"] if item["id"] == task.id)

    assert restored_task["entries"][-1]["change_paths"] == []


def test_workspace_binding_cannot_race_with_message_start(tmp_path: Path) -> None:
    old_workspace = tmp_path / "old"
    new_workspace = tmp_path / "new"
    old_workspace.mkdir()
    new_workspace.mkdir()
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    project = runtime.add_project(str(old_workspace))
    task = runtime.new_conversation(project["id"])
    original_add_project = runtime.add_project

    def racing_add_project(raw_path: str) -> dict[str, Any]:
        runtime.send_message(task["id"], "start during binding")
        return original_add_project(raw_path)

    runtime.add_project = racing_add_project  # type: ignore[method-assign]
    rebound = runtime.bind_workspace(task["id"], str(new_workspace))

    assert rebound["workspace"] == str(new_workspace.resolve())
    assert rebound["running"] is False


def test_add_project_with_conversation_persists_one_combined_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    saves = 0
    original_save = runtime._save

    def counting_save() -> None:
        nonlocal saves
        saves += 1
        original_save()

    runtime._save = counting_save  # type: ignore[method-assign]
    project, task = runtime.add_project_with_conversation(str(workspace))

    assert saves == 1
    assert project["path"] == str(workspace.resolve())
    assert task["workspace"] == str(workspace.resolve())


def test_permission_mode_is_per_conversation_and_new_tasks_inherit_last_choice(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    first = runtime.new_conversation()
    second = runtime.new_conversation()

    updated = runtime.set_permission_mode(first["id"], "full")
    third = runtime.new_conversation()

    state = runtime.snapshot()
    second_state = next(item for item in state["tasks"] if item["id"] == second["id"])
    assert updated["permission_mode"] == "full"
    assert second_state["permission_mode"] == "risk"
    assert third["permission_mode"] == "full"


def test_permission_mode_persists_across_runtime_restart(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    task = runtime.new_conversation()
    runtime.set_permission_mode(task["id"], "request")

    restored = WebRuntime(LocalSettings.load(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    restored_task = next(item for item in restored.snapshot()["tasks"] if item["id"] == task["id"])

    assert restored_task["permission_mode"] == "request"


def test_running_conversation_rejects_permission_change(tmp_path: Path) -> None:
    class BlockingModel:
        def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] | None = None) -> AssistantResponse:
            time.sleep(0.2)
            return AssistantResponse("done")

    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=BlockingModel)
    task = runtime.new_conversation()
    runtime.send_message(task["id"], "run")

    with pytest.raises(RuntimeConflict, match="权限"):
        runtime.set_permission_mode(task["id"], "full")

    runtime.cancel(task["id"])
    wait_until_idle(runtime, task["id"])


def test_permission_change_restores_saved_default_when_session_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    task = runtime.new_conversation()
    previous = runtime.settings.approval_mode
    monkeypatch.setattr(runtime.store, "save", lambda _state: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        runtime.set_permission_mode(task["id"], "full")

    assert runtime.settings.approval_mode == previous
    assert LocalSettings.load(tmp_path).approval_mode == previous


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


def test_remove_project_moves_conversations_to_projectless_and_keeps_files(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    project = runtime.add_project(str(tmp_path))
    task = runtime.new_conversation(project["id"])
    keep = tmp_path / "keep.txt"
    keep.write_text("do not touch", encoding="utf-8")

    runtime.remove_project(project["id"])

    state = runtime.snapshot()
    assert state["projects"] == []
    surviving = next(item for item in state["tasks"] if item["id"] == task["id"])
    assert surviving["project_id"] is None
    assert keep.read_text(encoding="utf-8") == "do not touch"


def test_remove_conversation_deletes_only_that_conversation(tmp_path: Path) -> None:
    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=lambda: ScriptedModel([]))
    first = runtime.new_conversation()
    second = runtime.new_conversation()

    runtime.remove_conversation(first["id"])

    state = runtime.snapshot()
    assert [item["id"] for item in state["tasks"]] == [second["id"]]


def test_remove_running_conversation_is_rejected(tmp_path: Path) -> None:
    class BlockingModel:
        def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] | None = None) -> AssistantResponse:
            time.sleep(0.2)
            return AssistantResponse("done")

    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=BlockingModel)
    task = runtime.new_conversation()
    runtime.send_message(task["id"], "run")

    with pytest.raises(RuntimeConflict, match="正在运行"):
        runtime.remove_conversation(task["id"])

    runtime.cancel(task["id"])
    wait_until_idle(runtime, task["id"])


def test_remove_project_with_running_conversation_is_rejected(tmp_path: Path) -> None:
    class BlockingModel:
        def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] | None = None) -> AssistantResponse:
            time.sleep(0.2)
            return AssistantResponse("done")

    runtime = WebRuntime(settings(tmp_path), tmp_path, model_factory=BlockingModel)
    project = runtime.add_project(str(tmp_path))
    task = runtime.new_conversation(project["id"])
    runtime.send_message(task["id"], "run")

    with pytest.raises(RuntimeConflict, match="运行"):
        runtime.remove_project(project["id"])

    runtime.cancel(task["id"])
    wait_until_idle(runtime, task["id"])
