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
    [
        "git reset --hard HEAD",
        "git clean -fd",
        "git push --force origin main",
        "git push --force-with-lease",
        "git push origin --delete old",
        "git push origin :old",
        "shutdown /s",
        "sudo rm file",
        "rm ../outside.txt",
    ],
)
def test_denied_commands(policy: CommandPolicy, command: str) -> None:
    assert policy.classify(command).level == RiskLevel.DENY


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content README.md > copied.txt",
        'Get-Content README.md | python -c "print(1)"',
        "git status; echo changed",
        "pwd && echo changed",
        "Get-Content $(Get-Item README.md)",
        'Get-Content `"README.md`"',
        "Get-Content README.md (python payload)",
        "Get-Content README.md @(python payload)",
    ],
)
def test_compound_commands_are_not_treated_as_read_only(policy: CommandPolicy, command: str) -> None:
    assert policy.is_read_only(command) is False
    assert policy.classify(command).level == RiskLevel.REVIEW


def test_git_global_options_cannot_bypass_destructive_denial(policy: CommandPolicy) -> None:
    assert policy.classify("git -C . reset --hard HEAD~1").level == RiskLevel.DENY
