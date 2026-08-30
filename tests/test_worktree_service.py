from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_agent.worktree_service import WorktreeOperationError, WorktreeService


def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "tests@example.invalid")
    git(path, "config", "user.name", "Coding Agent Tests")
    git(path, "config", "commit.gpgsign", "false")
    (path / "app.txt").write_text("main\n", encoding="utf-8")
    git(path, "add", "app.txt")
    git(path, "commit", "-m", "initial")
    return path


def test_create_maps_nested_workspace_to_independent_branch(tmp_path: Path) -> None:
    root = repository(tmp_path / "repository")
    nested = root / "packages" / "api"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "packages/api/module.py")
    git(root, "commit", "-m", "add package")

    result = WorktreeService(nested, tmp_path / "state").create("abc123def4567890")
    isolated = Path(result["workspace"])

    assert result["branch"] == "coding-agent/task-abc123def456"
    assert isolated.relative_to(Path(result["worktree_root"])) == Path("packages/api")
    assert isolated.joinpath("module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    isolated.joinpath("module.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert nested.joinpath("module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert git(isolated, "branch", "--show-current").stdout.strip() == result["branch"]
    assert git(root, "branch", "--show-current").stdout.strip() != result["branch"]


def test_create_rejects_non_repository(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()

    with pytest.raises(WorktreeOperationError) as caught:
        WorktreeService(workspace, tmp_path / "state").create("abc123")

    assert caught.value.code == "not_repository"
