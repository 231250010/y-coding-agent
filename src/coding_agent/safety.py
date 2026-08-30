from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class RiskLevel(str, Enum):
    SAFE = "safe"
    REVIEW = "review"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class CommandDecision:
    level: RiskLevel
    reason: str


class CommandPolicy:
    """Conservative application-level command classifier, not an OS sandbox."""

    _destructive = re.compile(
        r"(?i)(git\b[^\r\n;&|]*?\bpush\b[^\r\n;&|]*(?:"
        r"--force(?:-with-lease|-if-includes)?\b|\s-f(?:\s|$)|--delete\b|\s:\S+)|"
        r"git\b[^\r\n;&|]*?\b(reset\s+--hard|clean\s+-[^\s]*f)|"
        r"shutdown|reboot|restart-computer|stop-computer|format(?:\.com)?\b|diskpart\b|"
        r"reg\s+delete|set-executionpolicy|sudo\b|runas\b)"
    )
    _delete = re.compile(r"(?i)(^|[;&|]\s*)(rm\b|del\b|erase\b|rmdir\b|remove-item\b)")
    _root_delete = re.compile(
        r"(?i)(rm\s+-(?:\w*r\w*f|\w*f\w*r)\s+[/~]|"
        r"remove-item\b[^\r\n]*(?:-recurse)[^\r\n]*(?:[A-Z]:\\\s*(?:$|[;&|])))"
    )
    _outside_hint = re.compile(r"(?:^|[\s\\/])\.\.(?:[\\/]|$)")
    _network_or_install = re.compile(
        r"(?i)\b(pip(?:3)?\s+install|uv\s+(?:add|pip)|poetry\s+add|"
        r"npm\s+(?:install|i|add)|pnpm\s+(?:install|add)|yarn\s+add|"
        r"cargo\s+install|curl\b|wget\b|invoke-webrequest\b|irm\b|ssh\b|scp\b)"
    )
    _mutation = re.compile(
        r"(?i)\b(git\s+(?:commit|push|pull|merge|rebase|checkout|switch|branch)|"
        r"mkdir\b|new-item\b|move-item\b|copy-item\b|mv\b|cp\b|touch\b|"
        r"set-content\b|add-content\b|chmod\b|chown\b)"
    )
    _safe = re.compile(
        r"(?i)^\s*(?:"
        r"pwd|ls(?:\s|$)|dir(?:\s|$)|get-childitem(?:\s|$)|get-content(?:\s|$)|"
        r"type(?:\s|$)|cat(?:\s|$)|head(?:\s|$)|tail(?:\s|$)|"
        r"rg(?:\s|$)|grep(?:\s|$)|findstr(?:\s|$)|"
        r"git\s+(?:status|diff|log|show)(?:\s|$)|"
        r"pytest(?:\s|$)|python(?:3)?\s+-m\s+(?:pytest|unittest|compileall|py_compile)(?:\s|$)|"
        r"npm\s+(?:test|run\s+(?:test|build|lint))(?:\s|$)|"
        r"pnpm\s+(?:test|run\s+(?:test|build|lint))(?:\s|$)|"
        r"yarn\s+(?:test|run\s+(?:test|build|lint))(?:\s|$)|"
        r"cargo\s+(?:test|check|build)(?:\s|$)|go\s+(?:test|build)(?:\s|$)"
        r")"
    )
    _read_only = re.compile(
        r"(?i)^\s*(?:"
        r"pwd|ls(?:\s|$)|dir(?:\s|$)|get-childitem(?:\s|$)|get-content(?:\s|$)|"
        r"type(?:\s|$)|cat(?:\s|$)|head(?:\s|$)|tail(?:\s|$)|"
        r"rg(?:\s|$)|grep(?:\s|$)|findstr(?:\s|$)|"
        r"git\s+(?:status|diff|log|show)(?:\s|$)"
        r")"
    )
    _shell_composition = re.compile(r"[|><;&`(){}]|\$\(|@\(")

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def classify(self, command: str) -> CommandDecision:
        command = command.strip()
        if not command:
            return CommandDecision(RiskLevel.DENY, "空命令")
        if "\x00" in command or "\n" in command or "\r" in command:
            return CommandDecision(RiskLevel.DENY, "命令包含不允许的控制字符")
        if self._destructive.search(command) or self._root_delete.search(command):
            return CommandDecision(RiskLevel.DENY, "检测到不可恢复、提权或系统级操作")
        if self._delete.search(command) and self._outside_hint.search(command):
            return CommandDecision(RiskLevel.DENY, "拒绝删除工作区外路径")
        if self._shell_composition.search(command):
            return CommandDecision(RiskLevel.REVIEW, "命令包含未解析的管道、重定向或复合表达式")
        if self._network_or_install.search(command):
            return CommandDecision(RiskLevel.REVIEW, "命令可能联网或安装依赖")
        if self._delete.search(command) or self._mutation.search(command):
            return CommandDecision(RiskLevel.REVIEW, "命令可能修改或删除本地内容")
        if self._outside_hint.search(command):
            return CommandDecision(RiskLevel.REVIEW, "命令可能访问工作区外路径")
        if self._safe.search(command):
            return CommandDecision(RiskLevel.SAFE, "已识别的只读、测试或构建命令")
        return CommandDecision(RiskLevel.REVIEW, "未识别命令需要人工确认")

    def is_read_only(self, command: str) -> bool:
        """Return whether a command is in the narrow, known read-only allowlist."""
        stripped = command.strip()
        return not self._shell_composition.search(stripped) and bool(self._read_only.search(stripped))

    def classify_argv(self, command: Sequence[str]) -> CommandDecision:
        """Classify a shell-free argument vector used by release policy checks."""
        if (
            isinstance(command, (str, bytes))
            or not command
            or len(command) > 30
            or any(not isinstance(part, str) or not part for part in command)
        ):
            return CommandDecision(RiskLevel.DENY, "门禁命令参数数组无效")
        if any("\x00" in part or "\n" in part or "\r" in part for part in command):
            return CommandDecision(RiskLevel.DENY, "门禁命令包含不允许的控制字符")

        executable = Path(command[0]).name.casefold()
        if executable in {
            "cmd",
            "cmd.exe",
            "powershell",
            "powershell.exe",
            "pwsh",
            "bash",
            "sh",
            "zsh",
            "wsl",
            "sudo",
        }:
            return CommandDecision(RiskLevel.DENY, "发布门禁禁止启动 Shell、提权或子系统解释器")
        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            if len(command) >= 3 and command[1] == "-m" and command[2] in {
                "pytest",
                "unittest",
                "compileall",
                "py_compile",
            }:
                return self._argv_path_decision(command)
            if len(command) >= 2 and command[1] in {"-c", "-"}:
                return CommandDecision(RiskLevel.DENY, "发布门禁禁止内联 Python 代码")
            return CommandDecision(RiskLevel.REVIEW, "非标准 Python 检查需要人工确认")

        rendered = subprocess.list2cmdline(list(command))
        decision = self.classify(rendered)
        if decision.level is RiskLevel.DENY:
            return decision
        if executable in {
            "pytest",
            "pytest.exe",
            "npm",
            "npm.cmd",
            "pnpm",
            "pnpm.cmd",
            "yarn",
            "yarn.cmd",
            "cargo",
            "cargo.exe",
            "go",
            "go.exe",
            "git",
            "git.exe",
        } and decision.level is RiskLevel.SAFE:
            return self._argv_path_decision(command)
        return CommandDecision(RiskLevel.REVIEW, "未列入发布门禁安全清单，需要人工确认")

    def _argv_path_decision(self, command: Sequence[str]) -> CommandDecision:
        executable_path = Path(command[0])
        if executable_path.parent != Path("."):
            return CommandDecision(RiskLevel.REVIEW, "门禁命令使用了带路径的可执行文件")
        for argument in command[1:]:
            candidate = Path(argument)
            if ".." in candidate.parts or candidate.is_absolute():
                return CommandDecision(RiskLevel.REVIEW, "门禁命令可能访问工作区外路径")
        return CommandDecision(RiskLevel.SAFE, "已识别的测试、构建或只读门禁命令")
