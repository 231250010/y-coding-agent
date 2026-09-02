from __future__ import annotations

import json
import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import coding_agent.directory_picker as directory_picker
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


def test_windows_child_prefers_native_dialog_without_loading_tk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "project"
    selected.mkdir()
    native_calls: list[str | None] = []

    def native_picker(initial: str | None) -> str | None:
        native_calls.append(initial)
        return str(selected)

    monkeypatch.setattr(directory_picker.sys, "platform", "win32")
    monkeypatch.setattr(directory_picker, "_choose_directory_windows", native_picker)
    monkeypatch.setattr(
        directory_picker,
        "_choose_directory_tk",
        lambda _initial: pytest.fail("native picker should not load Tk"),
    )

    result = directory_picker._choose_directory(str(tmp_path))

    assert result == str(selected.resolve())
    assert native_calls == [str(tmp_path)]


def test_windows_child_returns_none_when_native_dialog_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(directory_picker.sys, "platform", "win32")
    monkeypatch.setattr(
        directory_picker, "_choose_directory_windows", lambda _initial: None
    )
    monkeypatch.setattr(
        directory_picker,
        "_choose_directory_tk",
        lambda _initial: pytest.fail("cancel should not open a second dialog"),
    )

    result = directory_picker._choose_directory(None)

    assert result is None


def test_windows_child_falls_back_to_tk_when_native_dialog_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "fallback"
    selected.mkdir()
    tk_calls: list[str | None] = []

    def native_picker(_initial: str | None) -> str | None:
        raise DirectoryPickerError("native unavailable")

    def tk_picker(initial: str | None) -> str | None:
        tk_calls.append(initial)
        return str(selected)

    monkeypatch.setattr(directory_picker.sys, "platform", "win32")
    monkeypatch.setattr(directory_picker, "_choose_directory_windows", native_picker)
    monkeypatch.setattr(directory_picker, "_choose_directory_tk", tk_picker)

    result = directory_picker._choose_directory(str(tmp_path))

    assert result == str(selected)
    assert tk_calls == [str(tmp_path)]
