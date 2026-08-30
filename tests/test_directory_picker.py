from __future__ import annotations

import json
import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from coding_agent.directory_picker import DirectoryPickerError, pick_directory


def test_picker_reads_selected_directory_from_isolated_child(tmp_path: Path) -> None:
    selected = tmp_path / "project"
    selected.mkdir()
    calls: list[list[str]] = []
    options: dict[str, object] = {}

    def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append(command)
        options.update(kwargs)
        return CompletedProcess(command, 0, json.dumps({"path": str(selected)}), "")

    assert pick_directory(str(tmp_path), runner=runner) == str(selected.resolve())
    assert "--child" in calls[0]
    assert calls[0][-2:] == ["--initial", str(tmp_path)]
    assert isinstance(options["env"], dict)
    assert options["env"]["PYTHONIOENCODING"] == "utf-8"
    assert options["env"]["PATH"] == os.environ["PATH"]


def test_picker_returns_none_when_user_cancels() -> None:
    def runner(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, json.dumps({"path": None}), "")

    assert pick_directory(None, runner=runner) is None


def test_picker_rejects_malformed_child_response() -> None:
    def runner(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, "not-json", "")

    with pytest.raises(DirectoryPickerError, match="目录选择器返回了无效结果"):
        pick_directory(None, runner=runner)
