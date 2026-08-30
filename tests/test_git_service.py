from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_agent.git_service import GitOperationError, GitService


def git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, encoding="utf-8", check=True
    )


def init_repo(path: Path) -> None:
    git(path, "init")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "commit.gpgsign", "false")


def test_status_stage_commit_diff_and_log_are_structured(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    service = GitService(tmp_path)

    initial = service.status()
    assert initial["files"] == [{"path": "a.txt", "index": "?", "worktree": "?"}]

    assert service.stage(["a.txt"])["count"] == 1
    assert "new file mode" in service.diff("staged")["diff"]
    commit = service.commit("feat: add a")
    assert len(commit["commit"]) == 40
    assert service.status()["files"] == []
    assert service.log(1)["commits"][0]["subject"] == "feat: add a"


def test_unstage_preserves_worktree_for_initial_commit(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    service = GitService(tmp_path)

    service.stage(["a.txt"])
    service.unstage(["a.txt"])

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert service.status()["files"] == [{"path": "a.txt", "index": "?", "worktree": "?"}]


def test_branches_and_create_branch(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    service = GitService(tmp_path)
    service.stage(["a.txt"])
    service.commit("initial")

    service.create_branch("feature/test")

    status = service.status()
    assert status["branch"] == "feature/test"
    assert any(item["name"] == "feature/test" and item["current"] for item in service.branches()["branches"])


def test_paths_cannot_escape_workspace_or_become_options(tmp_path: Path) -> None:
    init_repo(tmp_path)
    service = GitService(tmp_path)

    with pytest.raises(GitOperationError, match="超出工作区"):
        service.stage(["../outside.txt"])
    with pytest.raises(GitOperationError, match="路径无效"):
        service.stage(["-n"])


def test_not_repository_has_stable_error_code(tmp_path: Path) -> None:
    service = GitService(tmp_path)
    for operation in (service.status, service.log, service.branches):
        with pytest.raises(GitOperationError) as raised:
            operation()
        assert raised.value.code == "not_repository"


def test_remote_output_redacts_url_credentials_and_tokens() -> None:
    value = "https://user:secret@example.invalid/repo?access_token=top-secret&x=1"

    redacted = GitService._redact(value)

    assert "user:secret" not in redacted
    assert "top-secret" not in redacted
    assert "https://***@example.invalid/repo?access_token=***&x=1" == redacted


def test_pull_and_push_with_local_bare_remote(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    first = tmp_path / "first"
    second = tmp_path / "second"
    remote.mkdir()
    first.mkdir()
    git(remote, "init", "--bare")
    init_repo(first)
    (first / "a.txt").write_text("one\n", encoding="utf-8")
    service = GitService(first)
    service.stage(["a.txt"])
    service.commit("initial")
    git(first, "remote", "add", "origin", str(remote))

    pushed = service.push()
    assert pushed["upstream"].startswith("origin/")

    git(tmp_path, "clone", str(remote), str(second))
    git(second, "config", "user.email", "test@example.invalid")
    git(second, "config", "user.name", "Test User")
    git(second, "config", "commit.gpgsign", "false")
    (second / "a.txt").write_text("two\n", encoding="utf-8")
    git(second, "add", "a.txt")
    git(second, "commit", "-m", "remote update")
    git(second, "push")

    pulled = service.pull()
    assert pulled["updated"] is True
    assert (first / "a.txt").read_text(encoding="utf-8") == "two\n"
