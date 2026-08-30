from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.changes import ConversationChangeTracker, build_diff_rows


def test_new_file_is_reported_relative_to_workspace(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    capture = tracker.capture_paths(["src/new.py"])
    target = tmp_path / "src" / "new.py"
    target.parent.mkdir()
    target.write_text("one\ntwo\n", encoding="utf-8")

    changes = tracker.finish(capture)

    assert changes.paths == ("src/new.py",)
    change = tracker.changes["src/new.py"]
    assert change.status == "added"
    assert (change.added, change.deleted) == (2, 0)


def test_repeated_edits_stay_relative_to_first_baseline(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("old\n", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)

    first = tracker.capture_paths(["app.py"])
    path.write_text("middle\n", encoding="utf-8")
    tracker.finish(first)
    second = tracker.capture_paths(["app.py"])
    path.write_text("new\nextra\n", encoding="utf-8")
    tracker.finish(second)

    change = tracker.changes["app.py"]
    assert (change.added, change.deleted) == (2, 1)
    assert change.segments[0].baseline.text == "old\n"
    assert change.segments[0].latest.text == "new\nextra\n"


def test_new_turn_uses_current_file_as_baseline_without_losing_cumulative_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.py"
    tracker = ConversationChangeTracker(tmp_path)

    tracker.begin_turn()
    created = tracker.capture_paths(["app.py"])
    path.write_text("first\n", encoding="utf-8")
    tracker.finish(created)
    assert tracker.turn_changes["app.py"].status == "added"

    tracker.begin_turn()
    edited = tracker.capture_paths(["app.py"])
    path.write_text("second\n", encoding="utf-8")
    tracker.finish(edited)

    assert tracker.changes["app.py"].status == "added"
    assert tracker.turn_changes["app.py"].status == "modified"
    assert tracker.turn_changes["app.py"].segments[0].baseline.text == "first\n"
    assert tracker.turn_changes["app.py"].segments[0].latest.text == "second\n"


def test_reverting_to_baseline_removes_active_change(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("base\n", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    first = tracker.capture_paths(["a.txt"])
    path.write_text("changed\n", encoding="utf-8")
    tracker.finish(first)
    second = tracker.capture_paths(["a.txt"])
    path.write_text("base\n", encoding="utf-8")
    tracker.finish(second)
    assert tracker.changes == {}


def test_deleted_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "old.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    capture = tracker.capture_paths(["old.txt"])
    path.unlink()

    tracker.finish(capture)

    change = tracker.changes["old.txt"]
    assert change.status == "deleted"
    assert (change.added, change.deleted) == (0, 2)


def test_diff_rows_include_old_and_new_line_numbers() -> None:
    rows = build_diff_rows("one\nold\n", "one\nnew\nextra\n")
    removed = next(row for row in rows if row.kind == "removed")
    added = [row for row in rows if row.kind == "added"]
    assert (removed.old_line, removed.new_line, removed.text) == (2, None, "old")
    assert [(row.old_line, row.new_line, row.text) for row in added] == [
        (None, 2, "new"),
        (None, 3, "extra"),
    ]
    assert rows[0].kind == "hunk"


def test_binary_and_large_files_keep_metadata_without_text(tmp_path: Path) -> None:
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"a\x00b")
    large = tmp_path / "large.txt"
    large.write_text("1234", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path, max_text_bytes=4)
    capture = tracker.capture_paths(["blob.bin", "large.txt"])
    binary.write_bytes(b"a\x00c")
    large.write_text("12345", encoding="utf-8")

    tracker.finish(capture)

    assert tracker.changes["blob.bin"].binary is True
    assert tracker.changes["blob.bin"].segments[0].latest.text is None
    assert tracker.changes["large.txt"].truncated is True


def test_serialized_changes_round_trip(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    capture = tracker.capture_paths(["a.txt"])
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    tracker.finish(capture)

    restored = ConversationChangeTracker(tmp_path)
    restored.load_serialized(tracker.serialize())

    assert restored.serialize() == tracker.serialize()


def test_invalid_serialized_items_are_skipped(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    tracker.load_serialized([{"path": "../escape", "segments": []}, {"path": 42}, 42])
    assert tracker.changes == {}


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    with pytest.raises(ValueError, match="超出工作区"):
        tracker.capture_paths(["../outside.txt"])


def test_external_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "change-tracker-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前系统不允许创建符号链接")
    tracker = ConversationChangeTracker(tmp_path)
    with pytest.raises(ValueError, match="超出工作区"):
        tracker.capture_paths(["link.txt"])


def test_workspace_capture_finds_multiple_changes_and_ignores_dependencies(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("old\n", encoding="utf-8")
    ignored = tmp_path / "node_modules" / "pkg"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("old", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    capture = tracker.capture_workspace()

    (tmp_path / "old.txt").write_text("new\n", encoding="utf-8")
    (tmp_path / "made.txt").write_text("made\n", encoding="utf-8")
    (ignored / "index.js").write_text("new", encoding="utf-8")
    changes = tracker.finish(capture)

    assert changes.paths == ("made.txt", "old.txt")
    assert "node_modules/pkg/index.js" not in tracker.changes


def test_workspace_capture_reports_total_snapshot_limit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a" * 10, encoding="utf-8")
    (tmp_path / "b.txt").write_text("b" * 10, encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path, max_command_bytes=12)
    capture = tracker.capture_workspace()
    assert capture.warning is not None
    assert "预览不完整" in capture.warning


def test_external_drift_closes_old_segment_and_starts_new_one(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("base\n", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    capture = tracker.capture_paths(["a.txt"])
    path.write_text("agent one\n", encoding="utf-8")
    tracker.finish(capture)
    serialized = tracker.serialize()

    path.write_text("user edit\n", encoding="utf-8")
    restored = ConversationChangeTracker(tmp_path)
    restored.load_serialized(serialized)
    next_capture = restored.capture_paths(["a.txt"])
    path.write_text("agent two\n", encoding="utf-8")
    restored.finish(next_capture)

    change = restored.changes["a.txt"]
    assert len(change.segments) == 2
    assert change.segments[0].drifted is True
    assert change.segments[1].baseline.text == "user edit\n"


def test_retargeting_workspace_starts_a_labeled_segment(tmp_path: Path) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    tracker = ConversationChangeTracker(first_workspace)
    first = tracker.capture_paths(["same.txt"])
    (first_workspace / "same.txt").write_text("one\n", encoding="utf-8")
    tracker.finish(first)

    tracker.retarget(second_workspace)
    second = tracker.capture_paths(["same.txt"])
    (second_workspace / "same.txt").write_text("two\n", encoding="utf-8")
    tracker.finish(second)

    segments = tracker.changes["same.txt"].segments
    assert [segment.workspace for segment in segments] == [str(first_workspace), str(second_workspace)]
