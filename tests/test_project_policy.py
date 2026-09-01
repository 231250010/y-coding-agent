from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.project_policy import (
    MAX_PROJECT_RULE_BYTES,
    ProjectPolicyError,
    load_project_policy,
)


def test_missing_project_policy_is_empty(tmp_path: Path) -> None:
    assert load_project_policy(tmp_path).rules == ""
    assert load_project_policy(tmp_path).validation_commands == ()


def test_loads_rules_and_top_level_validation_alongside_devops_config(
    tmp_path: Path,
) -> None:
    rules_dir = tmp_path / ".coding-agent"
    rules_dir.mkdir()
    (rules_dir / "rules.md").write_text(
        "# Project conventions\nRun focused tests first.\n", encoding="utf-8"
    )
    (tmp_path / "coding-agent.toml").write_text(
        """validation = [
  ["python", "-m", "pytest", "-q"],
  ["cargo", "check"],
]

[devops]
default_environment = "development"
""",
        encoding="utf-8",
    )

    policy = load_project_policy(tmp_path)

    assert policy.rules == "# Project conventions\nRun focused tests first."
    assert policy.validation_commands == (
        ("python", "-m", "pytest", "-q"),
        ("cargo", "check"),
    )


@pytest.mark.parametrize(
    "validation",
    [
        'validation = "python -m pytest"\n',
        "validation = [[]]\n",
        'validation = [["python", "-c", "print(1)"]]\n',
        'validation = [["powershell", "-Command", "Get-ChildItem"]]\n',
    ],
)
def test_rejects_invalid_or_denied_validation_commands(
    tmp_path: Path, validation: str
) -> None:
    (tmp_path / "coding-agent.toml").write_text(validation, encoding="utf-8")

    with pytest.raises(ProjectPolicyError, match="validation"):
        load_project_policy(tmp_path)


def test_rejects_oversized_or_non_utf8_rules(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".coding-agent"
    rules_dir.mkdir()
    rules = rules_dir / "rules.md"
    rules.write_bytes(b"x" * (MAX_PROJECT_RULE_BYTES + 1))
    with pytest.raises(ProjectPolicyError, match="超过"):
        load_project_policy(tmp_path)

    rules.write_bytes(b"\xff")
    with pytest.raises(ProjectPolicyError, match="UTF-8"):
        load_project_policy(tmp_path)


def test_rejects_rules_symlink_that_escapes_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-rules.md"
    outside.write_text("outside", encoding="utf-8")
    rules_dir = tmp_path / ".coding-agent"
    rules_dir.mkdir()
    try:
        os.symlink(outside, rules_dir / "rules.md")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(ProjectPolicyError, match="工作区外"):
        load_project_policy(tmp_path)
