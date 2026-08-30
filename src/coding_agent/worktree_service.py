from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


WorktreeRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]


class WorktreeOperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.output = output


class WorktreeService:
    """Creates task-scoped Git worktrees without switching the source checkout."""

    def __init__(
        self,
        workspace: Path,
        state_root: Path,
        runner: WorktreeRunner | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.state_root = state_root.resolve()
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(
        command: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GCM_INTERACTIVE"] = "Never"
        return subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout,
            env=environment,
            check=False,
        )

    def create(self, task_id: str) -> dict[str, Any]:
        if not task_id or len(task_id) > 128 or not task_id.isalnum():
            raise WorktreeOperationError("invalid_task", "任务标识无效")
        root = self._repository_root()
        relative_workspace = self.workspace.relative_to(root)
        base_commit = self._call(["rev-parse", "HEAD"], root).stdout.strip()
        if not base_commit:
            raise WorktreeOperationError("unborn_repository", "仓库还没有提交，无法创建隔离工作区")
        base_branch_result = self._call_raw(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], root
        )
        base_branch = base_branch_result.stdout.strip() or None
        branch = f"coding-agent/task-{task_id[:12].lower()}"
        checked = self._call_raw(["check-ref-format", "--branch", branch], root)
        if checked.returncode != 0:
            raise WorktreeOperationError("invalid_branch", "无法生成安全的任务分支名")
        existing = self._call_raw(["show-ref", "--verify", f"refs/heads/{branch}"], root)
        if existing.returncode == 0:
            raise WorktreeOperationError("branch_exists", f"隔离分支已存在: {branch}")

        repository_key = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:16]
        worktree_root = self.state_root / repository_key / task_id.lower()
        if worktree_root.exists():
            raise WorktreeOperationError("path_exists", "隔离工作区目录已存在，请检查本机会话状态")
        worktree_root.parent.mkdir(parents=True, exist_ok=True)
        self._call(
            ["worktree", "add", "--no-track", "-b", branch, str(worktree_root), base_commit],
            root,
            timeout=120,
        )
        isolated_workspace = (worktree_root / relative_workspace).resolve()
        if not isolated_workspace.is_dir():
            raise WorktreeOperationError("worktree_invalid", "Git 已创建 worktree，但原工作目录映射不存在")
        return {
            "repository_root": str(root),
            "source_workspace": str(self.workspace),
            "worktree_root": str(worktree_root),
            "workspace": str(isolated_workspace),
            "branch": branch,
            "base_branch": base_branch,
            "base_commit": base_commit,
        }

    def _repository_root(self) -> Path:
        result = self._call_raw(["rev-parse", "--show-toplevel"], self.workspace)
        if result.returncode != 0 or not result.stdout.strip():
            raise WorktreeOperationError("not_repository", "当前工作目录不在 Git 仓库中")
        root = Path(result.stdout.strip()).resolve()
        try:
            self.workspace.relative_to(root)
        except ValueError as exc:
            raise WorktreeOperationError("unsafe_repository", "Git 仓库根目录与工作目录不一致") from exc
        return root

    def _call(
        self, arguments: Sequence[str], cwd: Path, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(["git", *arguments], cwd, timeout)
        except FileNotFoundError as exc:
            raise WorktreeOperationError("git_not_found", "未找到 Git 可执行文件") from exc
        except subprocess.TimeoutExpired as exc:
            raise WorktreeOperationError("git_timeout", "Git worktree 操作超时") from exc
        except OSError as exc:
            raise WorktreeOperationError("git_failed", f"无法启动 Git: {exc}") from exc
        if result.returncode != 0:
            output = self._combined_output(result)
            raise WorktreeOperationError(
                "git_failed", output or "Git worktree 操作失败", output=output
            )
        return result

    def _call_raw(
        self, arguments: Sequence[str], cwd: Path, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(["git", *arguments], cwd, timeout)
        except FileNotFoundError as exc:
            raise WorktreeOperationError("git_not_found", "未找到 Git 可执行文件") from exc
        except subprocess.TimeoutExpired as exc:
            raise WorktreeOperationError("git_timeout", "Git worktree 操作超时") from exc
        except OSError as exc:
            raise WorktreeOperationError("git_failed", f"无法启动 Git: {exc}") from exc

    @staticmethod
    def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
        return "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )[:4000]
