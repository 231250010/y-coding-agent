from pathlib import Path

from coding_agent.web import build_parser as build_web_parser


def test_web_parser() -> None:
    args = build_web_parser().parse_args(["--workspace", ".", "--port", "8123", "--no-browser"])
    assert args.workspace == Path(".")
    assert args.port == 8123
    assert args.no_browser is True


def test_dependency_and_entrypoint_boundary() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8").lower()
    assert '"openai>=' in text
    assert 'coding-agent = "coding_agent.web:main"' in text
    assert "coding-agent-cli" not in text
    assert '"rich' not in text
    assert "coding_agent.cli" not in text
    assert "coding_agent.gui" not in text
    assert "[project.gui-scripts]" not in text
    assert 'coding_agent = ["web_assets/*"]' in text
    for forbidden in [
        "openai-agents",
        "langchain",
        "llamaindex",
        "llama-index",
        "autogen",
        "crewai",
        "fastapi",
        "flask",
        "uvicorn",
    ]:
        assert forbidden not in text


def test_removed_frontends_do_not_remain_in_source_tree() -> None:
    package = Path(__file__).parents[1] / "src" / "coding_agent"
    assert not (package / "cli.py").exists()
    assert not (package / "gui.py").exists()
    assert not (package / "diff_view.py").exists()


def test_visual_companion_state_is_git_ignored() -> None:
    gitignore = Path(__file__).parents[1] / ".gitignore"
    assert ".superpowers/" in gitignore.read_text(encoding="utf-8").splitlines()
