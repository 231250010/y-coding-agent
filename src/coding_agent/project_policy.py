from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import CommandPolicy, RiskLevel


MAX_PROJECT_RULE_BYTES = 32_000
MAX_VALIDATION_COMMANDS = 12
MAX_VALIDATION_ARGUMENTS = 30
MAX_VALIDATION_ARGUMENT_CHARS = 4_096


class ProjectPolicyError(ValueError):
    """Raised when a workspace policy file is present but unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class ProjectPolicy:
    rules: str = ""
    validation_commands: tuple[tuple[str, ...], ...] = ()


def load_project_policy(workspace: Path) -> ProjectPolicy:
    root = workspace.resolve()
    rules = _read_optional_text(
        root,
        root / ".coding-agent" / "rules.md",
        label="项目规则",
        byte_limit=MAX_PROJECT_RULE_BYTES,
    )
    config_text = _read_optional_text(
        root,
        root / "coding-agent.toml",
        label="项目配置",
        byte_limit=256_000,
    )
    commands = _parse_validation_commands(root, config_text) if config_text else ()
    return ProjectPolicy(rules=rules.strip(), validation_commands=commands)


def _read_optional_text(
    workspace: Path,
    path: Path,
    *,
    label: str,
    byte_limit: int,
) -> str:
    if not path.exists():
        return ""
    if not path.is_file():
        raise ProjectPolicyError(f"{label}必须是普通文件: {path}")
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ProjectPolicyError(f"{label}不能通过符号链接指向工作区外") from exc
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ProjectPolicyError(f"无法读取{label}: {exc}") from exc
    if len(data) > byte_limit:
        raise ProjectPolicyError(f"{label}超过 {byte_limit} 字节限制")
    if b"\x00" in data:
        raise ProjectPolicyError(f"{label}必须是 UTF-8 文本，不能包含 NUL 字节")
    try:
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ProjectPolicyError(f"{label}必须使用 UTF-8 编码") from exc


def _parse_validation_commands(
    workspace: Path, config_text: str
) -> tuple[tuple[str, ...], ...]:
    try:
        parsed = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectPolicyError(f"coding-agent.toml 格式错误: {exc}") from exc
    raw = parsed.get("validation", [])
    if raw == []:
        return ()
    if not isinstance(raw, list):
        raise ProjectPolicyError("coding-agent.toml 的 validation 必须是参数数组列表")
    if len(raw) > MAX_VALIDATION_COMMANDS:
        raise ProjectPolicyError(
            f"validation 最多声明 {MAX_VALIDATION_COMMANDS} 条命令"
        )

    policy = CommandPolicy(workspace)
    commands: list[tuple[str, ...]] = []
    for index, value in enumerate(raw, start=1):
        command = _parse_command(value, index)
        decision = policy.classify_argv(command)
        if decision.level is RiskLevel.DENY:
            raise ProjectPolicyError(
                f"validation[{index}] 被安全策略拒绝: {decision.reason}"
            )
        if command not in commands:
            commands.append(command)
    return tuple(commands)


def _parse_command(value: Any, index: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_VALIDATION_ARGUMENTS
        or any(
            not isinstance(part, str)
            or not part
            or len(part) > MAX_VALIDATION_ARGUMENT_CHARS
            or "\x00" in part
            or "\n" in part
            or "\r" in part
            for part in value
        )
    ):
        raise ProjectPolicyError(
            f"validation[{index}] 必须是 1 到 {MAX_VALIDATION_ARGUMENTS} 项的有界非空字符串数组"
        )
    return tuple(value)
