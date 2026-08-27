from __future__ import annotations

import re
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
        r"(?i)(git\s+(reset\s+--hard|clean\s+-[^\s]*f)|"
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
        r"pytest(?:\s|$)|python(?:3)?\s+-m\s+(?:pytest|unittest|compileall)(?:\s|$)|"
        r"npm\s+(?:test|run\s+(?:test|build|lint))(?:\s|$)|"
        r"pnpm\s+(?:test|run\s+(?:test|build|lint))(?:\s|$)|"
        r"yarn\s+(?:test|run\s+(?:test|build|lint))(?:\s|$)|"
        r"cargo\s+(?:test|check|build)(?:\s|$)|go\s+(?:test|build)(?:\s|$)"
        r")"
    )

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
        if self._network_or_install.search(command):
            return CommandDecision(RiskLevel.REVIEW, "命令可能联网或安装依赖")
        if self._delete.search(command) or self._mutation.search(command):
            return CommandDecision(RiskLevel.REVIEW, "命令可能修改或删除本地内容")
        if self._outside_hint.search(command):
            return CommandDecision(RiskLevel.REVIEW, "命令可能访问工作区外路径")
        if self._safe.search(command):
            return CommandDecision(RiskLevel.SAFE, "已识别的只读、测试或构建命令")
        return CommandDecision(RiskLevel.REVIEW, "未识别命令需要人工确认")
