from __future__ import annotations

import threading
from pathlib import Path

import pytest

from coding_agent.changes import ConversationChangeTracker
from coding_agent.tools import PathGuard, ToolRegistry


def registry(tmp_path: Path, approval: bool = True, max_output: int = 16_000) -> ToolRegistry:
    return ToolRegistry(tmp_path, approver=lambda *_args: approval, max_output=max_output)


def test_projectless_registry_exposes_no_tools() -> None:
    registry = ToolRegistry(None)

    assert registry.workspace is None
    assert registry.schemas() == []
    result = registry.execute("read_file", {"path": "README.md"})
    assert result.ok is False
    assert result.error == "当前对话尚未选择工作目录"


def test_write_read_list_and_replace(tmp_path: Path) -> None:
    tools = registry(tmp_path)
    written = tools.execute("write_file", {"path": "src/你好.txt", "content": "第一行\n旧内容"})
    assert written.ok

    read = tools.execute("read_file", {"path": "src/你好.txt", "start_line": 2, "max_lines": 1})
    assert read.ok and "2: 旧内容" in read.output

    listed = tools.execute("list_files", {"path": ".", "pattern": "*.txt"})
    assert listed.ok and "src/你好.txt" in listed.output

    replaced = tools.execute(
        "replace_text",
        {"path": "src/你好.txt", "old_text": "旧内容", "new_text": "新内容"},
    )
    assert replaced.ok
    assert (tmp_path / "src" / "你好.txt").read_text(encoding="utf-8").endswith("新内容")


def test_write_and_replace_report_cumulative_local_changes(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)

    written = tools.execute("write_file", {"path": "a.txt", "content": "old\n"})
    replaced = tools.execute(
        "replace_text", {"path": "a.txt", "old_text": "old", "new_text": "new\nextra"}
    )

    assert written.changes.paths == ("a.txt",)
    assert replaced.changes.paths == ("a.txt",)
    assert (tracker.changes["a.txt"].added, tracker.changes["a.txt"].deleted) == (2, 0)
    assert "changes" not in written.to_message()


def test_failed_replace_that_does_not_write_has_no_changes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("same same", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, change_tracker=tracker)
    result = tools.execute("replace_text", {"path": "a.txt", "old_text": "same", "new_text": "x"})
    assert result.ok is False
    assert result.changes.paths == ()


def test_replace_requires_unique_match(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("same same", encoding="utf-8")
    tools = registry(tmp_path)
    result = tools.execute("replace_text", {"path": "a.txt", "old_text": "same", "new_text": "x"})
    assert not result.ok and "出现 2 次" in (result.error or "")
    result = tools.execute(
        "replace_text", {"path": "a.txt", "old_text": "same", "new_text": "x", "replace_all": True}
    )
    assert result.ok and path.read_text(encoding="utf-8") == "x x"


def test_path_traversal_and_absolute_escape_are_rejected(tmp_path: Path) -> None:
    guard = PathGuard(tmp_path)
    with pytest.raises(ValueError, match="超出工作区"):
        guard.resolve("../outside.txt")
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValueError, match="超出工作区"):
        guard.resolve(str(outside))


def test_external_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "external-target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前系统不允许创建符号链接")
    result = registry(tmp_path).execute("read_file", {"path": "link.txt"})
    assert not result.ok and "超出工作区" in (result.error or "")


def test_search_python_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("# needle\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr("coding_agent.tools.shutil.which", lambda _name: None)
    result = registry(tmp_path).execute(
        "search_text", {"query": "needle", "path": ".", "glob": "*.py", "max_results": 10}
    )
    assert result.ok
    assert "one.py:1" in result.output
    assert "two.txt" not in result.output


def test_unknown_and_invalid_arguments(tmp_path: Path) -> None:
    tools = registry(tmp_path)
    assert not tools.execute("missing", {}).ok
    result = tools.execute("read_file", {"path": "x", "extra": True})
    assert not result.ok and "未知参数" in (result.error or "")
    result = tools.execute("read_file", {"path": 123})
    assert not result.ok and "string" in (result.error or "")


def test_command_success_nonzero_and_output_truncation(tmp_path: Path) -> None:
    tools = registry(tmp_path, max_output=80)
    success = tools.execute(
        "run_command",
        {"command": "python -c \"print('x' * 200)\"", "timeout_seconds": 10},
    )
    assert success.ok and "已截断" in success.output
    failure = tools.execute(
        "run_command",
        {"command": "python -c \"import sys; sys.exit(7)\"", "timeout_seconds": 10},
    )
    assert not failure.ok and "exit_code=7" in failure.output


def test_command_approval_and_denial(tmp_path: Path) -> None:
    rejected = registry(tmp_path, approval=False).execute("run_command", {"command": "echo hello"})
    assert not rejected.ok and "未批准" in (rejected.error or "")
    denied = registry(tmp_path).execute("run_command", {"command": "git reset --hard HEAD"})
    assert not denied.ok and "安全策略拒绝" in (denied.error or "")


def test_command_timeout(tmp_path: Path) -> None:
    command = 'python -c "import time; time.sleep(3)"'
    result = registry(tmp_path).execute("run_command", {"command": command, "timeout_seconds": 1})
    assert not result.ok and "超过 1 秒" in (result.error or "")


def test_command_can_be_cancelled(tmp_path: Path) -> None:
    cancelled = threading.Event()
    tools = ToolRegistry(
        tmp_path,
        approver=lambda *_args: True,
        is_cancelled=cancelled.is_set,
    )
    timer = threading.Timer(0.2, cancelled.set)
    timer.start()
    try:
        result = tools.execute(
            "run_command",
            {"command": 'python -c "import time; time.sleep(5)"', "timeout_seconds": 10},
        )
    finally:
        timer.cancel()
    assert not result.ok and "用户停止" in (result.error or "")


def test_run_command_reports_created_modified_and_deleted_files(tmp_path: Path) -> None:
    (tmp_path / "edit.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("gone\n", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    command = (
        'python -c "from pathlib import Path; '
        "Path('edit.txt').write_text('after\\n'); "
        "Path('made.txt').write_text('made\\n'); Path('delete.txt').unlink()\""
    )

    result = tools.execute("run_command", {"command": command, "timeout_seconds": 10})

    assert result.ok is True
    assert result.changes.paths == ("delete.txt", "edit.txt", "made.txt")


def test_nonzero_command_still_reports_written_file(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    command = (
        'python -c "from pathlib import Path; import sys; '
        "Path('partial.txt').write_text('x'); sys.exit(7)\""
    )
    result = tools.execute("run_command", {"command": command, "timeout_seconds": 10})
    assert result.ok is False
    assert result.changes.paths == ("partial.txt",)
