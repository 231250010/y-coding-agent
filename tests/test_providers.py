from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from coding_agent.changes import ConversationChangeTracker
from coding_agent.providers import CompositeToolProvider, build_default_tool_provider
from coding_agent.tools import ToolResult


class StubProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": self.name, "parameters": {"type": "object"}},
            }
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(True, output=f"{name}:{arguments.get('value', '')}")


def test_composite_routes_tools_to_owning_provider() -> None:
    tools = CompositeToolProvider([StubProvider("first"), StubProvider("second")])

    assert [item["function"]["name"] for item in tools.schemas()] == ["first", "second"]
    assert tools.execute("second", {"value": "ok"}).output == "second:ok"


def test_composite_rejects_duplicate_and_unknown_tools() -> None:
    with pytest.raises(ValueError, match="重复工具"):
        CompositeToolProvider([StubProvider("same"), StubProvider("same")])

    assert CompositeToolProvider([]).execute("missing", {}).error == "未知工具: missing"


def test_default_provider_adds_git_and_devops_tools_for_selected_workspace(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    tracker = ConversationChangeTracker(tmp_path)

    tools = build_default_tool_provider(tmp_path, change_tracker=tracker)

    names = [item["function"]["name"] for item in tools.schemas()]
    assert names[:2] == ["list_files", "read_file"]
    assert "git_status" in names
    assert "git_push" in names
    assert "devops_inspect" in names
    assert "compose_deploy" in names
    assert "compose_verify" in names
    assert getattr(tools, "change_tracker", None) is tracker


def test_default_provider_keeps_projectless_conversations_tool_free() -> None:
    tools = build_default_tool_provider(None)

    assert tools.schemas() == []
    assert tools.execute("git_status", {}).error == "当前对话尚未选择工作目录"
