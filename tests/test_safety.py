from pathlib import Path

import pytest

from coding_agent.safety import CommandPolicy, RiskLevel


@pytest.fixture
def policy(tmp_path: Path) -> CommandPolicy:
    return CommandPolicy(tmp_path)


@pytest.mark.parametrize(
    "command",
    ["git status", "git diff", "rg TODO src", "python -m pytest", "npm run build"],
)
def test_safe_commands(policy: CommandPolicy, command: str) -> None:
    assert policy.classify(command).level == RiskLevel.SAFE


@pytest.mark.parametrize(
    "command",
    ["pip install thing", "curl https://example.invalid", "git commit -m test", "rm local.txt", "echo hello"],
)
def test_review_commands(policy: CommandPolicy, command: str) -> None:
    assert policy.classify(command).level == RiskLevel.REVIEW


@pytest.mark.parametrize(
    "command",
    ["git reset --hard HEAD", "git clean -fd", "shutdown /s", "sudo rm file", "rm ../outside.txt"],
)
def test_denied_commands(policy: CommandPolicy, command: str) -> None:
    assert policy.classify(command).level == RiskLevel.DENY

