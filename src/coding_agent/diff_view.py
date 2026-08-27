from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Any, Callable

from .changes import DiffRow, FileChange, build_diff_rows


@dataclass(frozen=True, slots=True)
class DiffPalette:
    surface: str
    canvas: str
    border: str
    text: str
    muted: str
    accent: str
    added_bg: str
    added_fg: str
    removed_bg: str
    removed_fg: str
    ui_font: str
    mono_font: str


def format_diff_row(row: DiffRow) -> str:
    if row.kind == "hunk":
        return f"             {row.text}\n"
    old = "" if row.old_line is None else str(row.old_line)
    new = "" if row.new_line is None else str(row.new_line)
    marker = "+" if row.kind == "added" else "-" if row.kind == "removed" else " "
    return f"{old:>5} {new:>5} {marker} {row.text}\n"


class FileChangeCard(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        change: FileChange,
        command: Callable[[str], None],
        palette: DiffPalette,
    ) -> None:
        super().__init__(
            parent,
            bg=palette.surface,
            highlightthickness=1,
            highlightbackground=palette.border,
            highlightcolor=palette.accent,
            takefocus=True,
            cursor="hand2",
        )
        self.change = change
        self.command = command
        status_icon = {"added": "＋", "deleted": "−", "modified": "▤"}.get(change.status, "▤")
        icon = tk.Label(
            self,
            text=status_icon,
            bg=palette.surface,
            fg=palette.accent,
            font=(palette.mono_font, 10, "bold"),
        )
        icon.pack(side="left", padx=(10, 4), pady=7)
        path_label = tk.Label(
            self,
            text=change.path,
            bg=palette.surface,
            fg=palette.text,
            font=(palette.mono_font, 9),
            anchor="w",
        )
        path_label.pack(side="left", fill="x", expand=True, pady=7)
        deleted_label = tk.Label(
            self,
            text=f"−{change.deleted}",
            bg=palette.surface,
            fg=palette.removed_fg,
            font=(palette.mono_font, 9, "bold"),
        )
        deleted_label.pack(side="right", padx=(2, 10))
        added_label = tk.Label(
            self,
            text=f"+{change.added}",
            bg=palette.surface,
            fg=palette.added_fg,
            font=(palette.mono_font, 9, "bold"),
        )
        added_label.pack(side="right", padx=(8, 2))
        for widget in (self, icon, path_label, added_label, deleted_label):
            widget.bind("<Button-1>", self._activate)
            widget.configure(cursor="hand2")
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", lambda _event: self.configure(highlightbackground=palette.accent))
        self.bind("<FocusOut>", lambda _event: self.configure(highlightbackground=palette.border))

    def _activate(self, _event: Any) -> str:
        self.command(self.change.path)
        return "break"


class DiffReviewPane(tk.Frame):
    def __init__(self, parent: tk.Misc, palette: DiffPalette, on_close: Callable[[], None]) -> None:
        super().__init__(parent, bg=palette.surface, highlightthickness=0)
        self.palette = palette
        self.on_close = on_close
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = tk.Frame(self, bg=palette.surface, padx=14, pady=11)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.path_label = tk.Label(
            header,
            text="",
            bg=palette.surface,
            fg=palette.text,
            font=(palette.mono_font, 10, "bold"),
            anchor="w",
        )
        self.path_label.grid(row=0, column=0, sticky="ew")
        self.added_label = tk.Label(
            header, text="+0", bg=palette.surface, fg=palette.added_fg, font=(palette.mono_font, 9, "bold")
        )
        self.added_label.grid(row=0, column=1, padx=(8, 3))
        self.deleted_label = tk.Label(
            header, text="−0", bg=palette.surface, fg=palette.removed_fg, font=(palette.mono_font, 9, "bold")
        )
        self.deleted_label.grid(row=0, column=2, padx=3)
        self.close_button = tk.Button(
            header,
            text="×",
            command=on_close,
            relief="flat",
            bd=0,
            bg=palette.surface,
            fg=palette.muted,
            activebackground=palette.canvas,
            activeforeground=palette.text,
            cursor="hand2",
            font=(palette.ui_font, 13, "bold"),
        )
        self.close_button.grid(row=0, column=3, padx=(8, 0))
        self.meta_label = tk.Label(
            header,
            text="当前对话累计改动",
            bg=palette.surface,
            fg=palette.muted,
            font=(palette.ui_font, 8),
            anchor="w",
        )
        self.meta_label.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.warning_label = tk.Label(
            self,
            text="",
            bg=palette.canvas,
            fg=palette.muted,
            font=(palette.ui_font, 8),
            anchor="w",
            justify="left",
            wraplength=420,
            padx=12,
            pady=7,
        )

        code = tk.Frame(self, bg=palette.surface)
        code.grid(row=2, column=0, sticky="nsew")
        code.rowconfigure(0, weight=1)
        code.columnconfigure(0, weight=1)
        self.text = tk.Text(
            code,
            wrap="none",
            relief="flat",
            bd=0,
            padx=0,
            pady=8,
            bg=palette.surface,
            fg=palette.text,
            insertbackground=palette.text,
            font=(palette.mono_font, 9),
            state="disabled",
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        vertical = tk.Scrollbar(code, orient="vertical", command=self.text.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = tk.Scrollbar(code, orient="horizontal", command=self.text.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.text.tag_configure("context", background=palette.surface, foreground=palette.text)
        self.text.tag_configure("added", background=palette.added_bg, foreground=palette.added_fg)
        self.text.tag_configure("removed", background=palette.removed_bg, foreground=palette.removed_fg)
        self.text.tag_configure("hunk", background=palette.canvas, foreground=palette.muted)
        self.text.tag_configure("workspace", background=palette.surface, foreground=palette.muted)

    def show_change(self, change: FileChange) -> None:
        self.path_label.configure(text=change.path)
        self.added_label.configure(text=f"+{change.added}")
        self.deleted_label.configure(text=f"−{change.deleted}")
        warnings = [value for value in (change.warning,) if value]
        if any(segment.drifted for segment in change.segments):
            warnings.append("工作区内容曾在 Agent 操作之外变化，以下 Diff 按追踪段展示。")
        if warnings:
            self.warning_label.configure(text="\n".join(warnings))
            self.warning_label.grid(row=1, column=0, sticky="ew")
        else:
            self.warning_label.grid_remove()

        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for index, segment in enumerate(change.segments, start=1):
            if len(change.segments) > 1 or segment.drifted:
                self.text.insert("end", f"工作区：{segment.workspace} · 追踪段 {index}\n", "workspace")
            if segment.baseline.text is None or segment.latest.text is None:
                reason = segment.latest.reason or segment.baseline.reason or "没有可用的文本预览"
                self.text.insert("end", reason + "\n", "context")
                continue
            rows = build_diff_rows(segment.baseline.text, segment.latest.text)
            if not rows:
                self.text.insert("end", "文件内容与该段基线一致。\n", "context")
            for row in rows:
                self.text.insert("end", format_diff_row(row), row.kind)
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.path_label.configure(text="")
        self.added_label.configure(text="+0")
        self.deleted_label.configure(text="−0")
        self.warning_label.grid_remove()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
