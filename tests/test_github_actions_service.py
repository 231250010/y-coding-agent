from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from coding_agent.github_actions_service import GitHubActionsError, GitHubActionsService


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: list[subprocess.CompletedProcess[str]] = []

    def add(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.responses.append(
            subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)
        )

    def __call__(self, command: Sequence[str], _cwd: Path, _timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return self.responses.pop(0)


def run(run_id: int, workflow: str, status: str, conclusion: str) -> dict[str, object]:
    return {
        "databaseId": run_id,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
        "headSha": "a" * 40,
        "url": f"https://github.test/actions/runs/{run_id}",
        "createdAt": "2026-08-30T00:00:00Z",
        "event": "push",
    }


def test_status_uses_head_and_latest_run_per_workflow(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("a" * 40)
    runner.add(json.dumps([
        run(30, "tests", "completed", "success"),
        run(29, "tests", "completed", "failure"),
        run(20, "lint", "completed", "success"),
    ]))

    result = GitHubActionsService(tmp_path, runner).status()

    assert result["overall"] == "success"
    assert result["successful"] is True
    assert [item["run_id"] for item in result["runs"]] == [30, 20]
    assert runner.calls[1][:4] == ["gh", "run", "list", "--commit"]


def test_required_workflow_missing_or_pending_blocks_success(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add(json.dumps([run(30, "tests", "completed", "success")]))
    service = GitHubActionsService(tmp_path, runner)

    missing = service.status("a" * 40, workflows=["tests", "lint"])

    assert missing["overall"] == "missing"
    assert missing["successful"] is False


def test_failed_logs_are_redacted_and_bounded(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("token=very-secret\n" + "x" * 2000)

    result = GitHubActionsService(tmp_path, runner).failed_logs(42, max_chars=1000)

    assert "very-secret" not in result["logs"]
    assert result["truncated"] is True
    assert runner.calls[0] == ["gh", "run", "view", "42", "--log-failed"]


def test_auth_failure_has_stable_error_code(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add(stderr="To get started with GitHub CLI, run: gh auth login", returncode=1)

    with pytest.raises(GitHubActionsError) as caught:
        GitHubActionsService(tmp_path, runner).status("a" * 40)

    assert caught.value.code == "github_auth_required"


def test_rerun_failed_uses_fixed_argument_vector(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add("queued")

    result = GitHubActionsService(tmp_path, runner).rerun_failed(123)

    assert result["status"] == "rerun_requested"
    assert runner.calls[0] == ["gh", "run", "rerun", "123", "--failed"]
