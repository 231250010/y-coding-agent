from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.changes import ConversationChangeTracker
from coding_agent.skills import SkillToolProvider


def write_skill(root: Path, directory: str, name: str, description: str, body: str) -> Path:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_skill_descriptions_are_exposed_before_full_instructions(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "testing", "test-helper", "运行项目测试", "先读取测试配置，再运行测试。")
    provider = SkillToolProvider([(root, "project")])

    schemas = provider.schemas()

    assert len(schemas) == 2
    load_schema = next(item for item in schemas if item["function"]["name"] == "load_skill")
    schema_text = json.dumps(load_schema, ensure_ascii=False)
    assert "test-helper" in schema_text
    assert "运行项目测试" in schema_text
    assert "先读取测试配置" not in schema_text

    result = provider.execute("load_skill", {"name": "test-helper"})
    assert result.ok is True
    assert "先读取测试配置，再运行测试" in result.output
    assert "从属于系统、用户和安全规则" in result.output


def test_skill_resources_are_listed_and_read_on_demand(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_path = write_skill(root, "testing", "test-helper", "运行项目测试", "Read references when needed.")
    reference = skill_path.parent / "references" / "pytest.md"
    reference.parent.mkdir()
    reference.write_text("pytest -q tests/test_core.py\n", encoding="utf-8")
    provider = SkillToolProvider([(root, "project")])

    loaded = provider.execute("load_skill", {"name": "test-helper"})

    assert "references/pytest.md" in loaded.output
    assert "pytest -q" not in loaded.output
    resource = provider.execute(
        "read_skill_resource",
        {"skill": "test-helper", "path": "references/pytest.md"},
    )
    assert resource.ok is True
    assert "pytest -q tests/test_core.py" in resource.output
    assert "未被执行" in resource.output


def test_skill_resource_rejects_traversal_binary_and_skill_file(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_path = write_skill(root, "one", "one", "one skill", "body")
    (skill_path.parent / "binary.bin").write_bytes(b"\xff\x00")
    provider = SkillToolProvider([(root, "project")])

    assert provider.execute(
        "read_skill_resource", {"skill": "one", "path": "../outside.txt"}
    ).ok is False
    assert provider.execute(
        "read_skill_resource", {"skill": "one", "path": "SKILL.md"}
    ).ok is False
    binary = provider.execute(
        "read_skill_resource", {"skill": "one", "path": "binary.bin"}
    )
    assert binary.ok is False
    assert "UTF-8" in str(binary.error)


def test_project_skill_overrides_same_named_local_skill(tmp_path: Path) -> None:
    local = tmp_path / "local"
    project = tmp_path / "project"
    write_skill(local, "shared", "shared", "本机版本", "local body")
    project_path = write_skill(project, "shared", "shared", "项目版本", "project body")

    provider = SkillToolProvider([(local, "local"), (project, "project")])

    assert provider.skills[0].path == project_path.resolve()
    result = provider.execute("load_skill", {"name": "shared"})
    assert "project body" in result.output
    assert "local body" not in result.output


def test_invalid_skill_frontmatter_is_not_discovered(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    bad = root / "bad" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("# no frontmatter\n", encoding="utf-8")

    provider = SkillToolProvider([(root, "project")])

    assert provider.schemas() == []
    assert provider.skills == ()


def test_load_skill_rejects_unknown_or_extra_arguments(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "one", "one", "one skill", "body")
    provider = SkillToolProvider([(root, "project")])

    assert provider.execute("load_skill", {"name": "missing"}).ok is False
    assert provider.execute("load_skill", {"name": "one", "extra": True}).ok is False


def test_skill_script_requires_approval_uses_minimal_env_and_tracks_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    skill_path = write_skill(root, "build", "build-helper", "生成本地文件", "Inspect scripts before running.")
    script = skill_path.parent / "scripts" / "generate.py"
    script.parent.mkdir()
    script.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('generated', encoding='utf-8')\n"
        "print('api_key=' + str(os.getenv('CODING_AGENT_API_KEY')))\n"
        "print('token=visible-value')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODING_AGENT_API_KEY", "must-not-reach-skill")
    approvals: list[str] = []
    tracker = ConversationChangeTracker(tmp_path)
    provider = SkillToolProvider(
        [(root, "project")],
        workspace=tmp_path,
        approver=lambda command, *_args: approvals.append(command) or True,
        change_tracker=tracker,
    )

    schemas = provider.schemas()
    assert any(item["function"]["name"] == "run_skill_script" for item in schemas)
    reviewed = provider.execute(
        "read_skill_resource",
        {"skill": "build-helper", "path": "scripts/generate.py"},
    )
    assert reviewed.ok is True and "sha256=" in reviewed.output
    result = provider.execute(
        "run_skill_script",
        {
            "skill": "build-helper",
            "path": "scripts/generate.py",
            "args": ["generated.txt"],
        },
    )

    assert result.ok is True
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "generated"
    assert result.changes.paths == ("generated.txt",)
    assert approvals and "generate.py" in approvals[0]
    assert "sha256=" in approvals[0]
    assert "must-not-reach-skill" not in result.output
    assert "visible-value" not in result.output
    assert "api_key=***" in result.output
    assert "token=***" in result.output


def test_skill_script_denial_or_traversal_never_executes(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_path = write_skill(root, "build", "build-helper", "生成本地文件", "body")
    script = skill_path.parent / "scripts" / "generate.py"
    script.parent.mkdir()
    script.write_text("from pathlib import Path\nPath('should-not-exist').write_text('x')\n", encoding="utf-8")
    provider = SkillToolProvider(
        [(root, "project")], workspace=tmp_path, approver=lambda *_args: False
    )

    unreviewed = provider.execute(
        "run_skill_script", {"skill": "build-helper", "path": "scripts/generate.py"}
    )
    provider.execute(
        "read_skill_resource",
        {"skill": "build-helper", "path": "scripts/generate.py"},
    )
    denied = provider.execute(
        "run_skill_script", {"skill": "build-helper", "path": "scripts/generate.py"}
    )
    traversal = provider.execute(
        "run_skill_script", {"skill": "build-helper", "path": "../generate.py"}
    )

    assert unreviewed.ok is False and "尚未审查" in str(unreviewed.error)
    assert denied.ok is False and "未批准" in str(denied.error)
    assert traversal.ok is False and "scripts/" in str(traversal.error)
    assert not (tmp_path / "should-not-exist").exists()


def test_skill_script_change_after_review_requires_new_review(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_path = write_skill(root, "build", "build-helper", "生成文件", "body")
    script = skill_path.parent / "scripts" / "generate.py"
    script.parent.mkdir()
    script.write_text("print('reviewed')\n", encoding="utf-8")
    approvals: list[str] = []
    provider = SkillToolProvider(
        [(root, "project")],
        workspace=tmp_path,
        approver=lambda command, *_args: approvals.append(command) or True,
    )

    reviewed = provider.execute(
        "read_skill_resource",
        {"skill": "build-helper", "path": "scripts/generate.py"},
    )
    script.write_text("print('replaced')\n", encoding="utf-8")
    result = provider.execute(
        "run_skill_script", {"skill": "build-helper", "path": "scripts/generate.py"}
    )

    assert reviewed.ok is True
    assert result.ok is False and "发生变化" in str(result.error)
    assert approvals == []


def test_skill_script_change_during_approval_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_path = write_skill(root, "build", "build-helper", "生成文件", "body")
    script = skill_path.parent / "scripts" / "generate.py"
    script.parent.mkdir()
    script.write_text("print('reviewed')\n", encoding="utf-8")

    def replace_during_approval(*_args: object) -> bool:
        script.write_text("print('changed during approval')\n", encoding="utf-8")
        return True

    provider = SkillToolProvider(
        [(root, "project")], workspace=tmp_path, approver=replace_during_approval
    )
    provider.execute(
        "read_skill_resource",
        {"skill": "build-helper", "path": "scripts/generate.py"},
    )

    result = provider.execute(
        "run_skill_script", {"skill": "build-helper", "path": "scripts/generate.py"}
    )

    assert result.ok is False and "审批期间" in str(result.error)
