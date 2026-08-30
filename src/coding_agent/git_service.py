from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


GitRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]


class GitOperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.output = output


class GitService:
    """Structured, non-shell Git operations scoped to one selected workspace."""

    def __init__(self, workspace: Path, runner: GitRunner | None = None) -> None:
        self.workspace = workspace.resolve()
        self._runner = runner or self._default_runner
        self._root: Path | None = None

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

    @property
    def repository_root(self) -> Path:
        if self._root is None:
            result = self._call_raw(["rev-parse", "--show-toplevel"], timeout=15)
            if result.returncode != 0:
                raise GitOperationError("not_repository", "当前工作目录不在 Git 仓库中")
            raw_root = result.stdout.strip()
            if not raw_root:
                raise GitOperationError("not_repository", "Git 未返回仓库根目录")
            root = Path(raw_root).resolve()
            try:
                self.workspace.relative_to(root)
            except ValueError as exc:
                raise GitOperationError("unsafe_operation", "工作目录不属于解析出的 Git 仓库") from exc
            self._root = root
        return self._root

    def status(self) -> dict[str, Any]:
        output = self._call(["status", "--porcelain=v1", "-z", "--branch"]).stdout
        records = output.split("\0")
        header = records.pop(0) if records else ""
        branch, upstream, ahead, behind, detached = self._parse_branch_header(header)
        files: list[dict[str, Any]] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 3:
                continue
            item: dict[str, Any] = {
                "path": record[3:],
                "index": record[0],
                "worktree": record[1],
            }
            if record[0] in {"R", "C"} or record[1] in {"R", "C"}:
                if index < len(records) and records[index]:
                    item["original_path"] = records[index]
                    index += 1
            files.append(item)
        return {
            "repository_root": str(self.repository_root),
            "branch": branch,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "detached": detached,
            "files": files,
        }

    def diff(self, scope: str = "workspace", path: str | None = None) -> dict[str, Any]:
        if scope not in {"workspace", "staged"}:
            raise GitOperationError("invalid_argument", "Diff 范围必须是 workspace 或 staged")
        args = ["diff", "--no-ext-diff", "--no-color"]
        if scope == "staged":
            args.append("--cached")
        normalized_path = self._validate_path(path) if path is not None else None
        if normalized_path is not None:
            args.extend(["--", normalized_path])
        output = self._call(args).stdout
        return {"scope": scope, "path": normalized_path, "diff": output}

    def log(self, limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise GitOperationError("invalid_argument", "提交数量必须在 1 到 100 之间")
        if not self._has_head():
            return {"commits": []}
        result = self._call(
            [
                "log",
                f"-{limit}",
                "--date=iso-strict",
                "--format=%H%x1f%h%x1f%an%x1f%ae%x1f%ad%x1f%s%x1e",
            ]
        )
        commits: list[dict[str, str]] = []
        for record in result.stdout.split("\x1e"):
            fields = record.strip().split("\x1f")
            if len(fields) == 6:
                commits.append(
                    dict(zip(("commit", "short", "author", "email", "date", "subject"), fields))
                )
        return {"commits": commits}

    def branches(self) -> dict[str, Any]:
        result = self._call(
            [
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)%00%(HEAD)%00%(upstream:short)%00%(objectname)",
                "refs/heads",
            ]
        )
        branches: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            fields = line.split("\0")
            if len(fields) == 4:
                branches.append(
                    {
                        "name": fields[0],
                        "current": fields[1] == "*",
                        "upstream": fields[2] or None,
                        "commit": fields[3],
                    }
                )
        return {"branches": branches}

    def create_branch(self, name: str) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 240 or any(char in name for char in "\r\n\0"):
            raise GitOperationError("invalid_argument", "分支名称无效")
        checked = self._call_raw(["check-ref-format", "--branch", name], timeout=15)
        if checked.returncode != 0:
            raise GitOperationError("invalid_argument", "分支名称不符合 Git 规则")
        self._call(["switch", "-c", name])
        return {"branch": name, "repository_root": str(self.repository_root)}

    def stage(self, paths: Sequence[str]) -> dict[str, Any]:
        normalized = self._validate_paths(paths)
        self._call(["add", "--", *normalized])
        return {"paths": normalized, "count": len(normalized)}

    def unstage(self, paths: Sequence[str]) -> dict[str, Any]:
        normalized = self._validate_paths(paths)
        if self._has_head():
            self._call(["restore", "--staged", "--", *normalized])
        else:
            self._call(["rm", "--cached", "--ignore-unmatch", "-r", "--", *normalized])
        return {"paths": normalized, "count": len(normalized)}

    def commit(self, message: str) -> dict[str, Any]:
        message = message.strip()
        if not message or len(message) > 500 or any(char in message for char in "\r\n\0"):
            raise GitOperationError("invalid_argument", "提交消息必须为 1 到 500 个字符的单行文本")
        self._call(["commit", "-m", message])
        commit = self._call(["rev-parse", "HEAD"]).stdout.strip()
        return {"commit": commit, "message": message}

    def pull(self) -> dict[str, Any]:
        before = self._head_or_none()
        result = self._call(["pull", "--ff-only"], timeout=120)
        after = self._head_or_none()
        return {
            "before": before,
            "after": after,
            "updated": before != after,
            "output": self._combined_output(result),
        }

    def push(self) -> dict[str, Any]:
        status = self.status()
        branch = status["branch"]
        if not branch or status["detached"]:
            raise GitOperationError("git_failed", "detached HEAD 状态下不能推送当前分支")
        upstream = self._call_raw(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], timeout=15
        )
        if upstream.returncode == 0:
            result = self._call(["push"], timeout=120)
            target = upstream.stdout.strip()
        else:
            remotes = self._call(["remote"]).stdout.splitlines()
            if "origin" not in remotes:
                raise GitOperationError("git_failed", "当前分支没有上游，且仓库没有 origin 远端")
            result = self._call(["push", "--set-upstream", "origin", branch], timeout=120)
            target = f"origin/{branch}"
        return {"branch": branch, "upstream": target, "output": self._combined_output(result)}

    def _validate_paths(self, paths: Sequence[str]) -> list[str]:
        if isinstance(paths, (str, bytes)) or not paths:
            raise GitOperationError("invalid_argument", "至少需要一个路径")
        return [self._validate_path(path) for path in paths]

    def _validate_path(self, path: str) -> str:
        if not isinstance(path, str) or not path.strip() or any(char in path for char in "\r\n\0"):
            raise GitOperationError("invalid_argument", "Git 路径无效")
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise GitOperationError("unsafe_operation", f"路径超出工作区: {path}")
        resolved = (self.workspace / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise GitOperationError("unsafe_operation", f"路径超出工作区: {path}") from exc
        normalized = relative.as_posix()
        if normalized == "." or normalized.startswith("-"):
            raise GitOperationError("invalid_argument", f"Git 路径无效: {path}")
        return normalized

    def _has_head(self) -> bool:
        self.repository_root
        return self._call_raw(["rev-parse", "--verify", "HEAD"], timeout=15).returncode == 0

    def _head_or_none(self) -> str | None:
        self.repository_root
        result = self._call_raw(["rev-parse", "--verify", "HEAD"], timeout=15)
        return result.stdout.strip() if result.returncode == 0 else None

    def _call(self, args: Sequence[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        self.repository_root
        result = self._call_raw(args, timeout=timeout)
        if result.returncode != 0:
            self._raise_failure(result)
        return result

    def _call_raw(
        self, args: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(["git", *args], self.workspace, timeout)
        except FileNotFoundError as exc:
            raise GitOperationError("git_unavailable", "本机未找到 Git 可执行文件") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitOperationError("git_failed", "Git 操作超时") from exc
        except OSError as exc:
            raise GitOperationError("git_failed", f"无法启动 Git: {exc}") from exc

    def _raise_failure(self, result: subprocess.CompletedProcess[str]) -> None:
        output = self._redact("\n".join(filter(None, (result.stdout.strip(), result.stderr.strip()))))
        lowered = output.lower()
        if "not a git repository" in lowered:
            code, message = "not_repository", "当前工作目录不在 Git 仓库中"
        elif "nothing to commit" in lowered or "no changes added to commit" in lowered:
            code, message = "nothing_to_commit", "没有可提交的暂存改动"
        elif any(term in lowered for term in ("authentication failed", "could not read username", "permission denied (publickey)")):
            code, message = "authentication_failed", "Git 远端认证失败"
        elif any(term in lowered for term in ("rejected", "failed to push some refs", "remote rejected")):
            code, message = "remote_rejected", "远端拒绝了 Git 操作"
        elif any(term in lowered for term in ("conflict", "not possible to fast-forward", "divergent branches")):
            code, message = "merge_conflict", "拉取无法快进或存在冲突"
        else:
            code, message = "git_failed", "Git 操作失败"
        raise GitOperationError(code, message, output=output)

    @staticmethod
    def _parse_branch_header(header: str) -> tuple[str | None, str | None, int, int, bool]:
        value = header[3:] if header.startswith("## ") else header
        ahead_match = re.search(r"\bahead (\d+)", value)
        behind_match = re.search(r"\bbehind (\d+)", value)
        ahead = int(ahead_match.group(1)) if ahead_match else 0
        behind = int(behind_match.group(1)) if behind_match else 0
        value = re.sub(r" \[[^]]+\]$", "", value)
        if value.startswith("No commits yet on "):
            return value.removeprefix("No commits yet on "), None, ahead, behind, False
        if value.startswith("Initial commit on "):
            return value.removeprefix("Initial commit on "), None, ahead, behind, False
        if value.startswith("HEAD ") or value == "HEAD":
            return None, None, ahead, behind, True
        branch, separator, upstream = value.partition("...")
        return branch or None, upstream if separator and upstream else None, ahead, behind, False

    @staticmethod
    def _redact(value: str) -> str:
        value = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1***@", value)
        return re.sub(
            r"(?i)([?&](?:access_token|token|auth|password|key)=)[^&\s]+",
            r"\1***",
            value,
        )

    @classmethod
    def _combined_output(cls, result: subprocess.CompletedProcess[str]) -> str:
        return cls._redact("\n".join(filter(None, (result.stdout.strip(), result.stderr.strip()))))
