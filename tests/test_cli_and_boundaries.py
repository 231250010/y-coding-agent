from pathlib import Path

import pytest

from coding_agent.cli import build_parser, main
from coding_agent.web import build_parser as build_web_parser


def test_cli_parser() -> None:
    args = build_parser().parse_args(["--model", "m", "--approval-mode", "full", "fix", "tests"])
    assert args.model == "m"
    assert args.approval_mode == "full"
    assert args.task == ["fix", "tests"]


@pytest.mark.parametrize("legacy", ["ask", "always"])
def test_cli_parser_accepts_legacy_approval_modes(legacy: str) -> None:
    assert build_parser().parse_args(["--approval-mode", legacy]).approval_mode == legacy


def test_web_parser() -> None:
    args = build_web_parser().parse_args(["--workspace", ".", "--port", "8123", "--no-browser"])
    assert args.workspace == Path(".")
    assert args.port == 8123
    assert args.no_browser is True


def test_cli_missing_key_returns_two(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODING_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    assert main(["--workspace", str(tmp_path), "task"]) == 2


def test_dependency_boundary() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8").lower()
    assert '"openai>=' in text
    assert 'coding-agent = "coding_agent.web:main"' in text
    assert 'coding-agent-cli = "coding_agent.cli:main"' in text
    assert '[project.gui-scripts]' not in text
    assert 'coding_agent = ["web_assets/*"]' in text
    for forbidden in [
        "openai-agents", "langchain", "llamaindex", "llama-index", "autogen", "crewai",
        "fastapi", "flask", "uvicorn",
    ]:
        assert forbidden not in text


def test_visual_companion_state_is_git_ignored() -> None:
    gitignore = Path(__file__).parents[1] / ".gitignore"
    assert ".superpowers/" in gitignore.read_text(encoding="utf-8").splitlines()
