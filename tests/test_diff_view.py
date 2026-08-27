from __future__ import annotations

import tkinter as tk

import pytest

from coding_agent.changes import ChangeSegment, FileChange, FileSnapshot, build_diff_rows
from coding_agent.diff_view import DiffPalette, FileChangeCard, format_diff_row


def sample_change() -> FileChange:
    return FileChange(
        path="src/app.py",
        segments=[
            ChangeSegment(
                workspace="C:/workspace",
                baseline=FileSnapshot(True, text="old\n"),
                latest=FileSnapshot(True, text="new\nextra\n"),
            )
        ],
        status="modified",
        added=2,
        deleted=1,
    )


def palette() -> DiffPalette:
    return DiffPalette(
        surface="#FFFFFF",
        canvas="#F7F5F0",
        border="#D9E0E5",
        text="#1E2D38",
        muted="#5D6A73",
        accent="#F2A97E",
        added_bg="#DDF4E5",
        added_fg="#177245",
        removed_bg="#FCE1E1",
        removed_fg="#B33A3A",
        ui_font="Microsoft YaHei UI",
        mono_font="Cascadia Mono",
    )


def test_formatted_diff_rows_have_two_line_number_columns() -> None:
    rows = build_diff_rows("one\nold\n", "one\nnew\nextra\n")
    removed = next(row for row in rows if row.kind == "removed")
    added = next(row for row in rows if row.kind == "added")
    assert format_diff_row(removed) == "    2       - old\n"
    assert format_diff_row(added) == "          2 + new\n"


def test_file_card_activates_with_keyboard_callback() -> None:
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display is unavailable")
    root.withdraw()
    called: list[str] = []
    try:
        card = FileChangeCard(root, sample_change(), called.append, palette())
        assert card._activate(None) == "break"
        assert called == ["src/app.py"]
    finally:
        root.destroy()
