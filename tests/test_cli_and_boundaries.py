from pathlib import Path

import pytest

from coding_agent.cli import build_parser, main
from coding_agent.gui import build_parser as build_gui_parser


def test_cli_parser() -> None:
    args = build_parser().parse_args(["--model", "m", "--approval-mode", "always", "fix", "tests"])
    assert args.model == "m"
    assert args.approval_mode == "always"
    assert args.task == ["fix", "tests"]


def test_gui_parser() -> None:
    args = build_gui_parser().parse_args(["--workspace", "."])
    assert args.workspace == Path(".")


def test_cli_missing_key_returns_two(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODING_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    assert main(["--workspace", str(tmp_path), "task"]) == 2


def test_dependency_boundary() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8").lower()
    assert '"openai>=' in text
    assert 'coding-agent = "coding_agent.gui:main"' in text
    assert 'coding-agent-cli = "coding_agent.cli:main"' in text
    for forbidden in ["openai-agents", "langchain", "llamaindex", "llama-index", "autogen", "crewai"]:
        assert forbidden not in text
