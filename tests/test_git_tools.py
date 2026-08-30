from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coding_agent.changes import ConversationChangeTracker
from coding_agent.git_service import GitOperationError, GitService
from coding_agent.git_tools import GitToolProvider


class StubGitService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str):
        def execute(*args: Any) -> dict[str, Any]:
            self.calls.append((name, args))
            return {"operation": name}

        return execute


def provider(mode: str, approvals: list[str]) -> GitToolProvider:
    return GitToolProvider(
        StubGitService(),  # type: ignore[arg-type]
        approval_mode=mode,
        approver=lambda command, _risk, _reason: approvals.append(command) or True,
    )


def valid_arguments(tool: str) -> dict[str, Any]:
    return {
        "git_status": {},
        "git_diff": {},
        "git_log": {},
        "git_branches": {},
        "git_create_branch": {"name": "feature/test"},
        "git_stage": {"paths": ["a.txt"]},
        "git_unstage": {"paths": ["a.txt"]},
        "git_commit": {"message": "test"},
        "git_pull": {},
        "git_push": {},
    }[tool]


@pytest.mark.parametrize(
    ("mode", "tool", "asks"),
    [
        ("request", "git_status", False),
        ("request", "git_commit", True),
        ("risk", "git_commit", False),
        ("risk", "git_push", True),
        ("full", "git_push", False),
    ],
)
def test_git_permission_matrix(mode: str, tool: str, asks: bool) -> None:
    approvals: list[str] = []

    result = provider(mode, approvals).execute(tool, valid_arguments(tool))

    assert result.ok is True
    assert bool(approvals) is asks


def test_schema_contains_only_narrow_non_destructive_surface() -> None:
    names = {item["function"]["name"] for item in provider("risk", []).schemas()}

    assert names == {
        "git_status", "git_diff", "git_log", "git_branches", "git_create_branch",
        "git_stage", "git_unstage", "git_commit", "git_pull", "git_push",
    }
    push = next(item for item in provider("risk", []).schemas() if item["function"]["name"] == "git_push")
    assert "force" not in push["function"]["parameters"]["properties"]


def test_arguments_are_validated_before_service_call() -> None:
    service = StubGitService()
    tools = GitToolProvider(service)  # type: ignore[arg-type]

    result = tools.execute("git_stage", {"paths": "a.txt", "force": True})

    assert result.ok is False
    assert "未知参数" in (result.error or "")
    assert service.calls == []


def test_service_error_is_returned_with_stable_code(tmp_path: Path) -> None:
    result = GitToolProvider(GitService(tmp_path)).execute("git_status", {})

    assert result.ok is False
    assert result.error == "not_repository: 当前工作目录不在 Git 仓库中"


def test_pull_reports_workspace_changes_even_when_service_writes(tmp_path: Path) -> None:
    class PullingService(StubGitService):
        def pull(self) -> dict[str, Any]:
            (tmp_path / "pulled.txt").write_text("new\n", encoding="utf-8")
            return {"updated": True}

    tracker = ConversationChangeTracker(tmp_path)
    tools = GitToolProvider(
        PullingService(),  # type: ignore[arg-type]
        approval_mode="full",
        change_tracker=tracker,
    )

    result = tools.execute("git_pull", {})

    assert result.ok is True
    assert result.changes.paths == ("pulled.txt",)
    assert json.loads(result.output)["updated"] is True
