from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ActionsRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]


class GitHubActionsError(RuntimeError):
    def __init__(self, code: str, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.output = output


class GitHubActionsService:
    """Narrow GitHub Actions integration backed by the authenticated gh CLI."""

    _FIELDS = "databaseId,workflowName,status,conclusion,headSha,url,createdAt,event"

    def __init__(
        self,
        workspace: Path,
        runner: ActionsRunner | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self._runner = runner
        self.is_cancelled = is_cancelled or (lambda: False)

    def status(
        self,
        commit: str | None = None,
        *,
        limit: int = 20,
        workflows: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise GitHubActionsError("invalid_argument", "运行数量必须在 1 到 100 之间")
        sha = self._commit(commit)
        result = self._run(
            [
                "gh",
                "run",
                "list",
                "--commit",
                sha,
                "--limit",
                str(limit),
                "--json",
                self._FIELDS,
            ],
            60,
        )
        try:
            loaded = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise GitHubActionsError(
                "github_invalid_response", "GitHub CLI 返回了无效 JSON"
            ) from exc
        if not isinstance(loaded, list):
            raise GitHubActionsError("github_invalid_response", "GitHub Actions 响应格式无效")
        wanted = set(workflows or [])
        runs: list[dict[str, Any]] = []
        seen_workflows: set[str] = set()
        for item in loaded:
            if not isinstance(item, dict):
                continue
            workflow = str(item.get("workflowName") or "")
            if wanted and workflow not in wanted:
                continue
            workflow_key = workflow or "unknown"
            if workflow_key in seen_workflows:
                continue
            run_id = item.get("databaseId")
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
                continue
            seen_workflows.add(workflow_key)
            runs.append(
                {
                    "run_id": run_id,
                    "workflow": workflow_key,
                    "status": str(item.get("status") or "unknown"),
                    "conclusion": str(item.get("conclusion") or ""),
                    "commit": str(item.get("headSha") or sha),
                    "url": str(item.get("url") or ""),
                    "event": str(item.get("event") or ""),
                    "created_at": str(item.get("createdAt") or ""),
                }
            )
        overall = self._overall(runs, wanted)
        return {
            "commit": sha,
            "overall": overall,
            "successful": overall == "success",
            "required_workflows": sorted(wanted),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "runs": runs,
        }

    def failed_logs(self, run_id: int, *, max_chars: int = 12_000) -> dict[str, Any]:
        run = self._run_id(run_id)
        if not 1000 <= max_chars <= 50_000:
            raise GitHubActionsError("invalid_argument", "日志字符数必须在 1000 到 50000 之间")
        result = self._run(["gh", "run", "view", str(run), "--log-failed"], 90)
        logs = self._redact("\n".join(filter(None, (result.stdout.strip(), result.stderr.strip()))))
        truncated = len(logs) > max_chars
        return {
            "run_id": run,
            "logs": logs[:max_chars],
            "truncated": truncated,
        }

    def rerun_failed(self, run_id: int) -> dict[str, Any]:
        run = self._run_id(run_id)
        result = self._run(["gh", "run", "rerun", str(run), "--failed"], 60)
        return {
            "run_id": run,
            "status": "rerun_requested",
            "output": self._redact(
                "\n".join(filter(None, (result.stdout.strip(), result.stderr.strip())))
            ),
        }

    def _commit(self, value: str | None) -> str:
        if value is not None:
            if not re.fullmatch(r"[a-fA-F0-9]{7,40}", value):
                raise GitHubActionsError("invalid_argument", "Commit SHA 无效")
            return value.lower()
        return self._run(["git", "rev-parse", "HEAD"], 15).stdout.strip().lower()

    @staticmethod
    def _run_id(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise GitHubActionsError("invalid_argument", "Workflow run ID 无效")
        return value

    @staticmethod
    def _overall(runs: Sequence[dict[str, Any]], wanted: set[str]) -> str:
        if not runs or (wanted and not wanted.issubset({item["workflow"] for item in runs})):
            return "missing"
        if any(item["status"] != "completed" for item in runs):
            return "pending"
        return "success" if all(item["conclusion"] == "success" for item in runs) else "failed"

    def _run(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if self.is_cancelled():
            raise GitHubActionsError("operation_cancelled", "GitHub Actions 操作已取消")
        try:
            result = (
                self._runner(command, self.workspace, timeout)
                if self._runner is not None
                else subprocess.run(
                    list(command),
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    timeout=timeout,
                    check=False,
                )
            )
        except FileNotFoundError as exc:
            raise GitHubActionsError(
                "github_cli_unavailable", "本机未安装 GitHub CLI（gh）"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubActionsError("github_timeout", "GitHub Actions 请求超时") from exc
        if self.is_cancelled():
            raise GitHubActionsError("operation_cancelled", "GitHub Actions 操作已取消")
        if result.returncode != 0:
            output = self._redact(
                "\n".join(filter(None, (result.stdout.strip(), result.stderr.strip())))
            )
            lowered = output.lower()
            if "auth login" in lowered or "not logged" in lowered or "authentication" in lowered:
                code, message = "github_auth_required", "GitHub CLI 尚未登录"
            elif "not a git repository" in lowered or "no remotes" in lowered:
                code, message = "github_repository_unavailable", "当前工作区没有可用的 GitHub 仓库"
            else:
                code, message = "github_actions_failed", "GitHub Actions 操作失败"
            raise GitHubActionsError(code, message, output=output)
        return result

    @staticmethod
    def _redact(value: str) -> str:
        return re.sub(
            r"(?i)((?:token|password|secret|authorization)\s*[=:]\s*)\S+",
            r"\1***",
            value,
        )
