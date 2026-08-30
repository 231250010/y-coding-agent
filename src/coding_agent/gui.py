from __future__ import annotations

import argparse
import queue
import threading
import tkinter as tk
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Any, Sequence

from .agent import AgentCancelled, AgentStopped, CodingAgent
from .changes import ConversationChangeTracker
from .config import Config, ConfigError
from .context import ContextManager
from .diff_view import DiffPalette, DiffReviewPane, FileChangeCard
from .local_settings import LocalSettings
from .model import ModelError, OpenAIChatModel
from .prompts import PROJECTLESS_SYSTEM_PROMPT, SYSTEM_PROMPT
from .safety import RiskLevel
from .session_store import SessionStore
from .tools import ToolRegistry


# Visual system: a blueberry project rail beside a soft, milk-white workspace.
# The friendly details stay concentrated in the mascot, status colors, and copy.
SIDEBAR = "#244A67"
SIDEBAR_RAISED = "#315B78"
SIDEBAR_TEXT = "#F5FAFC"
SIDEBAR_MUTED = "#C5D6DF"
CANVAS = "#F7F5F0"
SURFACE = "#FFFFFF"
BORDER = "#D9E0E5"
TEXT = "#1E2D38"
MUTED = "#5D6A73"
ACCENT = "#49697D"
ACCENT_HOVER = "#38566A"
SIGNATURE = "#F2A97E"
BLUSH = "#F2D6DA"
MINT = "#DDF4E5"
USER_BG = "#E9F0F5"
TOOL_BG = "#F2F6F4"
DIFF_ADDED_BG = "#DDF4E5"
DIFF_ADDED_FG = "#177245"
DIFF_REMOVED_BG = "#FCE1E1"
DIFF_REMOVED_FG = "#B33A3A"
ERROR = DIFF_REMOVED_FG
SUCCESS = DIFF_ADDED_FG
WARNING = "#95512F"
TOOL_STATUS = WARNING
UI_FONT = "Microsoft YaHei UI"
DISPLAY_FONT = "Microsoft YaHei UI"
MONO_FONT = "Cascadia Mono"
DIFF_PALETTE = DiffPalette(
    surface=SURFACE,
    canvas=CANVAS,
    border=BORDER,
    text=TEXT,
    muted=MUTED,
    accent=SIGNATURE,
    added_bg=DIFF_ADDED_BG,
    added_fg=DIFF_ADDED_FG,
    removed_bg=DIFF_REMOVED_BG,
    removed_fg=DIFF_REMOVED_FG,
    ui_font=UI_FONT,
    mono_font=MONO_FONT,
)
APP_NAME = "小码"
ASSISTANT_LABEL = APP_NAME
COMPOSER_LINES = 3
COMPOSER_SHORTCUT_HINT = "Enter 发送 · Shift+Enter 换行"
EMPTY_STATE = (
    "今天想让小码做点什么？",
    "说清目标，剩下的交给我慢慢理顺。",
    (
        "修复失败的测试，并解释原因",
        "读懂这个项目，告诉我从哪里开始",
        "优化当前代码，但不要改变功能",
    ),
)


def _pack_composer(composer: tk.Misc, transcript: tk.Misc) -> None:
    composer.pack(fill="x", side="bottom", before=transcript)


def _pack_composer_actions(
    menu_button: tk.Misc,
    workspace_label: tk.Misc,
    shortcut_label: tk.Misc,
    send_button: tk.Misc,
) -> None:
    send_button.pack(side="right")
    shortcut_label.pack(side="right", padx=(0, 12))
    menu_button.pack(side="left", padx=(0, 6))
    workspace_label.pack(side="left", fill="x", expand=True)


def normalize_display_name(value: str) -> str:
    return " ".join(value.replace("\n", " ").split())[:80]


@dataclass(slots=True)
class ChatEntry:
    kind: str
    text: str
    change_paths: tuple[str, ...] = ()


def collapse_change_entries(entries: list[ChatEntry]) -> list[str]:
    """Move legacy per-tool file links to the final entry of each user turn."""

    unfinished: list[str] = []

    def collapse_round(round_entries: list[ChatEntry]) -> None:
        paths: list[str] = []
        for entry in round_entries:
            for path in entry.change_paths:
                if path not in paths:
                    paths.append(path)
            entry.change_paths = ()
        if not paths:
            return
        target = next(
            (
                entry
                for entry in reversed(round_entries)
                if entry.kind in {"assistant", "error", "system"}
            ),
            None,
        )
        if target is None:
            unfinished.extend(path for path in paths if path not in unfinished)
        else:
            target.change_paths = tuple(paths)

    current_round: list[ChatEntry] = []
    for entry in entries:
        if entry.kind == "user" and current_round:
            collapse_round(current_round)
            current_round = []
        current_round.append(entry)
    collapse_round(current_round)
    return unfinished


@dataclass(slots=True)
class TaskSession:
    id: str
    project_id: str | None
    title: str
    agent: CodingAgent
    cancel_event: threading.Event
    change_tracker: ConversationChangeTracker = field(
        default_factory=lambda: ConversationChangeTracker(None)
    )
    entries: list[ChatEntry] = field(default_factory=list)
    running: bool = False
    title_is_custom: bool = False
    review_path: str | None = None
    pending_change_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProjectSession:
    id: str
    path: Path
    title: str


class ConfigDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, settings: LocalSettings, *, required: bool = False) -> None:
        super().__init__(parent)
        self.title("模型连接设置")
        self.configure(bg=CANVAS)
        self.resizable(False, False)
        # A transient window inherits the visibility of its parent on Windows.
        # The root is intentionally hidden during first-run setup, so only make
        # later settings dialogs transient.
        if not required:
            self.transient(parent)
        self.result: tuple[Config, LocalSettings] | None = None
        self.required = required

        self.variables = {
            "api_key": tk.StringVar(value=settings.api_key),
            "model": tk.StringVar(value=settings.model),
            "base_url": tk.StringVar(value=settings.base_url),
            "workspace": tk.StringVar(value=settings.workspace),
            "context_tokens": tk.StringVar(value=str(settings.context_tokens)),
            "max_steps": tk.StringVar(value=str(settings.max_steps)),
            "approval_mode": tk.StringVar(value=settings.approval_mode),
            "remember_key": tk.BooleanVar(value=settings.remember_key),
        }
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")

    def _build(self) -> None:
        body = tk.Frame(self, bg=CANVAS, padx=30, pady=26)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="模型连接", bg=CANVAS, fg=TEXT, font=(DISPLAY_FONT, 19)).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        tk.Label(
            body,
            text="连接信息仅保存在这台电脑上。",
            bg=CANVAS,
            fg=MUTED,
            font=(UI_FONT, 10),
        ).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(3, 18)
        )
        rows = [
            ("API Key", "api_key", True),
            ("模型", "model", False),
            ("Base URL", "base_url", False),
            ("上下文预算", "context_tokens", False),
            ("最大步骤", "max_steps", False),
        ]
        for index, (label, key, secret) in enumerate(rows, start=2):
            tk.Label(body, text=label, bg=CANVAS, fg=MUTED, anchor="w", width=12).grid(
                row=index, column=0, sticky="w", pady=6
            )
            entry = tk.Entry(
                body,
                textvariable=self.variables[key],
                show="•" if secret else "",
                width=44,
                bg=SURFACE,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=ACCENT,
            )
            entry.grid(row=index, column=1, sticky="ew", ipady=6, pady=6)

        option_row = len(rows) + 2
        tk.Label(body, text="命令审批", bg=CANVAS, fg=MUTED, anchor="w").grid(
            row=option_row, column=0, sticky="w", pady=6
        )
        approval = ttk.Combobox(
            body,
            textvariable=self.variables["approval_mode"],
            values=("request", "risk", "full"),
            state="readonly",
            width=41,
        )
        approval.grid(row=option_row, column=1, sticky="ew", pady=6)
        tk.Checkbutton(
            body,
            text="将 API Key 保存在本地忽略配置中",
            variable=self.variables["remember_key"],
            bg=CANVAS,
            fg=MUTED,
            activebackground=CANVAS,
            activeforeground=TEXT,
            selectcolor=SURFACE,
        ).grid(row=option_row + 1, column=1, sticky="w", pady=(6, 2))
        tk.Label(
            body,
            text="配置文件位于 .coding-agent/config.json，已被 Git 忽略。",
            bg=CANVAS,
            fg=MUTED,
            font=(UI_FONT, 9),
        ).grid(row=option_row + 2, column=1, sticky="w")

        buttons = tk.Frame(body, bg=CANVAS)
        buttons.grid(row=option_row + 3, column=0, columnspan=3, sticky="e", pady=(20, 0))
        if not self.required:
            tk.Button(
                buttons, text="取消", command=self._cancel, bg=SURFACE, fg=TEXT, relief="flat", padx=18, pady=8
            ).pack(side="left", padx=(0, 8))
        tk.Button(
            buttons,
            text="保存并连接",
            command=self._save,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_HOVER,
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=7,
        ).pack(side="left")

    def _save(self) -> None:
        try:
            settings = LocalSettings(
                api_key=self.variables["api_key"].get().strip(),
                model=self.variables["model"].get().strip(),
                base_url=self.variables["base_url"].get().strip(),
                workspace=self.variables["workspace"].get().strip(),
                context_tokens=int(self.variables["context_tokens"].get()),
                max_steps=int(self.variables["max_steps"].get()),
                approval_mode=self.variables["approval_mode"].get(),
                remember_key=bool(self.variables["remember_key"].get()),
            )
            config = Config.from_values(
                api_key=settings.api_key,
                model=settings.model,
                base_url=settings.base_url,
                workspace=settings.workspace,
                context_tokens=settings.context_tokens,
                max_steps=settings.max_steps,
                approval_mode=settings.approval_mode,
            )
        except (ConfigError, ValueError) as exc:
            messagebox.showerror("配置错误", str(exc), parent=self)
            return
        self.result = (config, settings)
        self.destroy()

    def _cancel(self) -> None:
        if self.required:
            self.destroy()
            self.master.destroy()
            return
        self.destroy()


class RenameDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, label: str, initial: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.configure(bg=CANVAS)
        self.resizable(False, False)
        self.transient(parent)
        self.result: str | None = None
        self.value = tk.StringVar(value=initial)

        body = tk.Frame(self, bg=CANVAS, padx=28, pady=24)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=title, bg=CANVAS, fg=TEXT, font=(DISPLAY_FONT, 17)).pack(anchor="w")
        tk.Label(body, text=label, bg=CANVAS, fg=MUTED, font=(UI_FONT, 10)).pack(anchor="w", pady=(12, 5))
        self.entry = tk.Entry(
            body,
            textvariable=self.value,
            width=42,
            bg=SURFACE,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(UI_FONT, 10),
        )
        self.entry.pack(fill="x", ipady=7)
        self.error_label = tk.Label(body, text="", bg=CANVAS, fg=ERROR, font=(UI_FONT, 9))
        self.error_label.pack(anchor="w", pady=(5, 0))
        buttons = tk.Frame(body, bg=CANVAS)
        buttons.pack(anchor="e", pady=(18, 0))
        tk.Button(
            buttons,
            text="取消",
            command=self._cancel,
            bg=SURFACE,
            fg=TEXT,
            activebackground=BLUSH,
            activeforeground=TEXT,
            relief="flat",
            padx=16,
            pady=7,
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            buttons,
            text="保存",
            command=self._save,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_HOVER,
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=7,
        ).pack(side="left")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", self._save)
        self.bind("<Escape>", self._cancel)
        self.grab_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.entry.focus_set()
        self.entry.selection_range(0, "end")

    def _save(self, _event: tk.Event[Any] | None = None) -> str:
        result = normalize_display_name(self.value.get())
        if not result:
            self.error_label.configure(text="名称不能为空")
            self.entry.focus_set()
            return "break"
        self.result = result
        self.destroy()
        return "break"

    def _cancel(self, _event: tk.Event[Any] | None = None) -> str:
        self.destroy()
        return "break"


class CodingAgentApp:
    def __init__(self, root: tk.Tk, config: Config, settings: LocalSettings, settings_root: Path) -> None:
        self.root = root
        self.config = config
        self.settings = settings
        self.settings_root = settings_root
        self.store = SessionStore(settings_root)
        self.projects: list[ProjectSession] = []
        self.tasks: list[TaskSession] = []
        self.current_id: str | None = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.closing = False
        self._hover_item: str | None = None
        self._active_menu: tk.Menu | None = None

        self._configure_window()
        self._build_layout()
        self._load_sessions()
        if not self.tasks:
            self.new_task()
        elif self.current_id is None:
            self.current_id = self.tasks[0].id
        self._refresh_task_tree()
        self._render_current()
        self.root.after(80, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Control-n>", lambda _event: self.new_task())
        self.root.bind("<Control-o>", lambda _event: self.choose_project())

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} · 本地代码工作台")
        self.root.geometry("1240x800")
        self.root.minsize(960, 640)
        self.root.configure(bg=CANVAS)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE,
            background=SURFACE,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
        )

    def _build_layout(self) -> None:
        split = tk.PanedWindow(self.root, orient="horizontal", bg=BORDER, sashwidth=1, bd=0)
        split.pack(fill="both", expand=True)

        sidebar = tk.Frame(split, bg=SIDEBAR, width=304)
        content = tk.Frame(split, bg=CANVAS)
        split.add(sidebar, minsize=260, width=304)
        split.add(content, minsize=680)

        side_header = tk.Frame(sidebar, bg=SIDEBAR, padx=18, pady=19)
        side_header.pack(fill="x")
        mark = tk.Label(
            side_header,
            text="◕‿◕",
            bg=SIGNATURE,
            fg=SIDEBAR,
            font=(DISPLAY_FONT, 12, "bold"),
            padx=10,
            pady=8,
        )
        mark.pack(side="left", padx=(0, 10))
        identity = tk.Frame(side_header, bg=SIDEBAR)
        identity.pack(side="left", fill="x", expand=True)
        tk.Label(
            identity,
            text=f"{APP_NAME}  CODING AGENT",
            bg=SIDEBAR,
            fg=SIDEBAR_TEXT,
            font=(DISPLAY_FONT, 12, "bold"),
        ).pack(anchor="w")
        tk.Label(
            identity,
            text="陪你慢慢把代码理顺",
            bg=SIDEBAR,
            fg=SIDEBAR_MUTED,
            font=(UI_FONT, 9),
        ).pack(anchor="w")

        section = tk.Frame(sidebar, bg=SIDEBAR, padx=18)
        section.pack(fill="x", pady=(10, 8))
        tk.Label(
            section,
            text="工作区",
            bg=SIDEBAR,
            fg=SIDEBAR_MUTED,
            font=(UI_FONT, 9, "bold"),
        ).pack(side="left")
        tk.Button(
            section,
            text="＋ 添加项目",
            command=self.choose_project,
            bg=SIDEBAR,
            fg=SIDEBAR_TEXT,
            activebackground=SIDEBAR_RAISED,
            activeforeground=SIDEBAR_TEXT,
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=4,
        ).pack(side="right")

        task_actions = tk.Frame(sidebar, bg=SIDEBAR, padx=14)
        task_actions.pack(fill="x", pady=(0, 10))
        self._sidebar_button(task_actions, "＋  开始新对话", self.new_task, primary=True).pack(fill="x")

        tree_style = ttk.Style(self.root)
        tree_style.configure(
            "Tasks.Treeview",
            background=SIDEBAR,
            fieldbackground=SIDEBAR,
            foreground=SIDEBAR_TEXT,
            borderwidth=0,
            rowheight=38,
            font=(UI_FONT, 10),
            indent=18,
            relief="flat",
            bordercolor=SIDEBAR,
            lightcolor=SIDEBAR,
            darkcolor=SIDEBAR,
            focuscolor=SIDEBAR,
        )
        tree_style.layout("Tasks.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        tree_style.map(
            "Tasks.Treeview",
            background=[("selected", SIDEBAR_RAISED)],
            foreground=[("selected", SIDEBAR_TEXT)],
        )
        self.task_tree = ttk.Treeview(
            sidebar,
            style="Tasks.Treeview",
            show="tree",
            selectmode="browse",
        )
        self.task_tree.tag_configure("section", foreground=SIDEBAR_MUTED, font=(UI_FONT, 9, "bold"))
        self.task_tree.tag_configure("project", foreground=SIDEBAR_MUTED, font=(UI_FONT, 9, "bold"))
        self.task_tree.tag_configure("task", foreground=SIDEBAR_TEXT, font=(UI_FONT, 10))
        self.task_tree.tag_configure("running", foreground="#F5B493", font=(UI_FONT, 10, "bold"))
        self.task_tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.task_tree.bind("<<TreeviewSelect>>", self._select_task)
        self.task_tree.bind("<Motion>", self._show_tree_actions)
        self.task_tree.bind("<Leave>", self._hide_tree_actions)
        self.task_tree.bind("<Button-3>", self._show_tree_actions)
        self.tree_actions = tk.Button(
            sidebar,
            text="···",
            bg=SIDEBAR_RAISED,
            fg=SIDEBAR_TEXT,
            activebackground="#485174",
            activeforeground=SIDEBAR_TEXT,
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=4,
            pady=0,
        )
        self.tree_actions.bind("<Enter>", lambda _event: None)
        self.tree_actions.bind("<Leave>", self._hide_tree_actions)
        self.tree_actions.place_forget()

        side_footer = tk.Frame(sidebar, bg=SIDEBAR, padx=14, pady=14)
        side_footer.pack(fill="x")
        self._sidebar_button(side_footer, "移除对话", self.delete_task).pack(side="left")
        self._sidebar_button(side_footer, "连接设置  ⚙", self.open_settings).pack(side="right")

        self.activity_bar = tk.Frame(content, bg=ACCENT, height=5)
        self.activity_bar.pack(fill="x")
        self.activity_bar.pack_propagate(False)

        header = tk.Frame(content, bg=CANVAS, padx=38, pady=20)
        header.pack(fill="x")
        title_stack = tk.Frame(header, bg=CANVAS)
        title_stack.pack(side="left", fill="x", expand=True)
        project_row = tk.Frame(title_stack, bg=CANVAS)
        project_row.pack(anchor="w")
        self.project_label = tk.Label(
            project_row,
            text="",
            bg=CANVAS,
            fg=ACCENT,
            font=(UI_FONT, 9, "bold"),
        )
        self.project_label.pack(side="left")
        self.project_menu_button = tk.Button(
            project_row,
            text="···",
            command=self._show_current_project_menu,
            bg=CANVAS,
            fg=ACCENT,
            activebackground=BLUSH,
            activeforeground=ACCENT_HOVER,
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=5,
            pady=0,
        )
        self.title_label = tk.Label(title_stack, text="", bg=CANVAS, fg=TEXT, font=(DISPLAY_FONT, 20))
        self.title_label.pack(anchor="w", pady=(2, 0))
        self.status_label = tk.Label(
            header,
            text="● 就绪",
            bg=CANVAS,
            fg=MUTED,
            font=(UI_FONT, 9, "bold"),
            padx=8,
            pady=4,
        )
        self.status_label.pack(side="right", padx=(12, 0))
        self.stop_button = tk.Button(
            header,
            text="停止",
            command=self.stop_task,
            state="disabled",
            bg="#F8E8E9",
            fg=ERROR,
            disabledforeground="#B9A8A5",
            activebackground=BLUSH,
            activeforeground=ERROR,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=7,
        )
        self.stop_button.pack(side="right")

        self.content_split = tk.PanedWindow(
            content,
            orient="horizontal",
            bg=BORDER,
            sashwidth=5,
            sashrelief="flat",
            bd=0,
        )
        self.content_split.pack(fill="both", expand=True)
        chat_content = tk.Frame(self.content_split, bg=CANVAS)
        self.content_split.add(chat_content, minsize=460, stretch="always")

        transcript_outer = tk.Frame(chat_content, bg=CANVAS, padx=38)
        transcript_outer.pack(fill="both", expand=True)
        transcript_frame = tk.Frame(transcript_outer, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        transcript_frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(transcript_frame, relief="flat", bd=0)
        scrollbar.pack(side="right", fill="y")
        self.transcript = tk.Text(
            transcript_frame,
            wrap="word",
            state="disabled",
            bg=SURFACE,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=0,
            font=(UI_FONT, 11),
            padx=32,
            pady=26,
            yscrollcommand=scrollbar.set,
        )
        self.transcript.pack(fill="both", expand=True)
        scrollbar.configure(command=self.transcript.yview)
        self._configure_transcript_tags()
        self._change_cards: list[FileChangeCard] = []

        composer = tk.Frame(chat_content, bg=CANVAS, padx=38, pady=14)
        _pack_composer(composer, transcript_outer)
        self.input_border = tk.Frame(composer, bg=BORDER, padx=1, pady=1)
        self.input_border.pack(fill="x")
        input_inner = tk.Frame(self.input_border, bg=SURFACE, padx=17, pady=11)
        input_inner.pack(fill="x")
        self.input_box = tk.Text(
            input_inner,
            height=COMPOSER_LINES,
            wrap="word",
            bg=SURFACE,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=0,
            font=(UI_FONT, 11),
            undo=True,
        )
        self.input_box.pack(fill="x")
        self.input_box.bind("<Return>", self._send_enter)
        self.input_box.bind("<Shift-Return>", self._insert_newline)
        self.input_box.bind("<Control-Return>", self._send_shortcut)
        self.input_box.bind("<FocusIn>", lambda _event: self.input_border.configure(bg=SIGNATURE))
        self.input_box.bind("<FocusOut>", lambda _event: self.input_border.configure(bg=BORDER))
        composer_actions = tk.Frame(input_inner, bg=SURFACE)
        composer_actions.pack(fill="x", pady=(8, 0))
        self.composer_menu_button = tk.Button(
            composer_actions,
            text="＋",
            command=self._show_composer_menu,
            bg=SURFACE,
            fg=ACCENT,
            activebackground=BLUSH,
            activeforeground=ACCENT_HOVER,
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=3,
            pady=0,
            font=(UI_FONT, 11, "bold"),
        )
        self.workspace_label = tk.Label(
            composer_actions,
            text=str(self.config.workspace),
            bg=SURFACE,
            fg=MUTED,
            font=(MONO_FONT, 9),
        )
        shortcut_label = tk.Label(
            composer_actions,
            text=COMPOSER_SHORTCUT_HINT,
            bg=SURFACE,
            fg=MUTED,
            font=(UI_FONT, 9),
        )
        self.send_button = tk.Button(
            composer_actions,
            text="开始工作  →",
            command=self.send_message,
            bg=SIGNATURE,
            fg=SIDEBAR,
            activebackground="#E98662",
            activeforeground=SIDEBAR,
            relief="flat",
            cursor="hand2",
            padx=18,
            pady=8,
        )
        _pack_composer_actions(self.composer_menu_button, self.workspace_label, shortcut_label, self.send_button)

        self.review_container = tk.Frame(
            self.content_split,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.review_pane = DiffReviewPane(self.review_container, DIFF_PALETTE, self._close_review)
        self.review_pane.pack(fill="both", expand=True)

    @staticmethod
    def _sidebar_button(parent: tk.Misc, text: str, command: Any, *, primary: bool = False) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=SIDEBAR_RAISED if primary else SIDEBAR,
            fg=SIDEBAR_TEXT if primary else SIDEBAR_MUTED,
            activebackground="#485174",
            activeforeground=SIDEBAR_TEXT,
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=12,
            pady=8 if primary else 5,
        )

    def _configure_transcript_tags(self) -> None:
        self.transcript.tag_configure("user_label", foreground=ACCENT, font=(UI_FONT, 9, "bold"), spacing1=16, spacing3=5)
        self.transcript.tag_configure("user", foreground=TEXT, background=USER_BG, lmargin1=16, lmargin2=16, rmargin=16, spacing1=9, spacing3=12)
        self.transcript.tag_configure("assistant_label", foreground=SUCCESS, font=(UI_FONT, 9, "bold"), spacing1=18, spacing3=5)
        self.transcript.tag_configure("assistant", foreground=TEXT, lmargin1=16, lmargin2=16, rmargin=16, spacing3=14)
        self.transcript.tag_configure("tool", foreground="#45616A", background=TOOL_BG, font=(MONO_FONT, 9), lmargin1=16, lmargin2=16, rmargin=16, spacing1=7, spacing3=7)
        self.transcript.tag_configure("error", foreground=ERROR, lmargin1=16, lmargin2=16, rmargin=16, spacing1=7, spacing3=7)
        self.transcript.tag_configure("system", foreground=MUTED, lmargin1=16, lmargin2=16, rmargin=16, spacing1=7, spacing3=7)
        self.transcript.tag_configure(
            "changes_header",
            foreground=MUTED,
            font=(UI_FONT, 9, "bold"),
            lmargin1=16,
            lmargin2=16,
            rmargin=16,
            spacing1=10,
            spacing3=5,
        )
        self.transcript.tag_configure("empty_face", foreground=SIGNATURE, font=(DISPLAY_FONT, 22, "bold"), justify="center", spacing1=60, spacing3=8)
        self.transcript.tag_configure("empty_title", foreground=TEXT, font=(DISPLAY_FONT, 18, "bold"), justify="center", spacing3=8)
        self.transcript.tag_configure("empty_body", foreground=MUTED, font=(UI_FONT, 10), justify="center", spacing3=5)
        self.transcript.tag_configure("empty_hint", foreground=ACCENT, font=(UI_FONT, 10), justify="center", spacing1=8, spacing3=4)

    def _make_agent(
        self,
        task_id: str,
        cancel_event: threading.Event,
        workspace: Path | None,
        change_tracker: ConversationChangeTracker,
    ) -> CodingAgent:
        model = OpenAIChatModel(
            api_key=self.config.api_key,
            model=self.config.model,
            base_url=self.config.base_url,
            timeout=self.config.request_timeout,
            max_retries=self.config.max_retries,
        )
        tools = ToolRegistry(
            workspace,
            approver=lambda command, risk, reason: self._request_approval(task_id, command, risk, reason),
            is_cancelled=cancel_event.is_set,
            approval_mode=self.config.approval_mode,
            change_tracker=change_tracker,
        )
        return CodingAgent(
            model,
            tools,
            ContextManager(self.config.context_tokens),
            max_steps=self.config.max_steps,
            on_event=lambda name, data: self.events.put(("agent_event", task_id, name, data)),
            is_cancelled=cancel_event.is_set,
            system_prompt=PROJECTLESS_SYSTEM_PROMPT if workspace is None else SYSTEM_PROMPT,
        )

    def choose_project(self) -> None:
        current_project = self._current_project()
        initial = current_project.path if current_project else Path(self.settings.workspace or self.config.workspace)
        selected = filedialog.askdirectory(parent=self.root, initialdir=str(initial), title="选择项目目录")
        if selected:
            self._add_project(Path(selected))

    def choose_workspace_for_current(self) -> None:
        session = self._current()
        if session is None:
            return
        if session.running:
            messagebox.showwarning("任务运行中", "请先停止任务，再更改工作目录。", parent=self.root)
            return
        project = self._find_project(session.project_id)
        initial = project.path if project else Path(self.settings.workspace or self.config.workspace)
        selected = filedialog.askdirectory(parent=self.root, initialdir=str(initial), title="选择工作目录")
        if not selected:
            return
        try:
            self._bind_task_to_path(session, Path(selected))
        except (OSError, ValueError) as exc:
            messagebox.showerror("工作目录无效", f"无法使用所选工作目录：{exc}", parent=self.root)

    def _add_project(self, path: Path) -> ProjectSession:
        project = self._ensure_project(path)
        project_tasks = [task for task in self.tasks if task.project_id == project.id]
        if project_tasks:
            self.current_id = project_tasks[0].id
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()
        return project

    def new_task(self, _project_id: str | None = None) -> None:
        task_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        change_tracker = ConversationChangeTracker(None)
        session = TaskSession(
            id=task_id,
            project_id=None,
            title=f"新对话 {len(self.tasks) + 1}",
            agent=self._make_agent(task_id, cancel_event, None, change_tracker),
            cancel_event=cancel_event,
            change_tracker=change_tracker,
        )
        self.tasks.append(session)
        self.current_id = task_id
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()
        self.input_box.focus_set()

    def _retarget_agent(self, session: TaskSession, project: ProjectSession | None) -> None:
        history = list(session.agent.history)
        workspace = project.path if project else None
        session.change_tracker.retarget(workspace)
        session.agent = self._make_agent(
            session.id,
            session.cancel_event,
            workspace,
            session.change_tracker,
        )
        session.agent.history = [session.agent.history[0], *history[1:]]

    def _ensure_project(self, path: Path) -> ProjectSession:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"工作目录不存在: {resolved}")
        existing = next((project for project in self.projects if project.path == resolved), None)
        if existing:
            return existing
        project = ProjectSession(id=uuid.uuid4().hex, path=resolved, title=resolved.name or str(resolved))
        self.projects.append(project)
        self.settings.workspace = str(resolved)
        return project

    def _bind_task_to_path(self, session: TaskSession, path: Path) -> ProjectSession:
        if session.running:
            raise RuntimeError("运行中的任务不能更改工作目录")
        project = self._ensure_project(path)
        session.project_id = project.id
        self._retarget_agent(session, project)
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()
        return project

    def _remove_project(self, project: ProjectSession) -> None:
        project_tasks = [task for task in self.tasks if task.project_id == project.id]
        if any(task.running for task in project_tasks):
            raise RuntimeError("运行中的任务不能移除工作目录")
        self.projects = [candidate for candidate in self.projects if candidate.id != project.id]
        for task in project_tasks:
            task.project_id = None
            self._retarget_agent(task, None)
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()

    def delete_task(self, item_id: str | None = None) -> None:
        if item_id is None:
            selected = self.task_tree.selection()
            if not selected:
                return
            item_id = selected[0]
        if item_id.startswith("project:"):
            project = self._find_project(item_id.removeprefix("project:"))
            if project is None:
                return
            project_tasks = [task for task in self.tasks if task.project_id == project.id]
            if any(task.running for task in project_tasks):
                messagebox.showwarning("项目运行中", "请先停止该项目中运行的对话。", parent=self.root)
                return
            if not messagebox.askyesno(
                "移除项目", f"从侧栏移除“{project.title}”吗？\n关联对话会保留，但不再有工作目录。", parent=self.root
            ):
                return
            self._remove_project(project)
            return
        if not item_id.startswith("task:"):
            return
        session = self._find_task(item_id.removeprefix("task:"))
        if session is None:
            return
        if session.running:
            messagebox.showwarning("任务运行中", "请先停止任务，再将其删除。", parent=self.root)
            return
        if not messagebox.askyesno("删除任务", f"确定删除“{session.title}”吗？", parent=self.root):
            return
        self.tasks = [task for task in self.tasks if task.id != session.id]
        if session.id != self.current_id:
            self._refresh_task_tree()
            self._save_sessions()
            return
        siblings = [task for task in self.tasks if task.project_id == session.project_id]
        if not siblings:
            self.current_id = None
            self.new_task()
            return
        self.current_id = siblings[-1].id
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()

    def send_message(self) -> None:
        session = self._current()
        text = self.input_box.get("1.0", "end-1c").strip()
        if session is None or not text or session.running:
            return
        self.input_box.delete("1.0", "end")
        if not session.title_is_custom and not session.entries:
            session.title = text.replace("\n", " ")[:32]
        session.pending_change_paths.clear()
        session.entries.append(ChatEntry("user", text))
        session.running = True
        session.cancel_event.clear()
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()
        worker = threading.Thread(target=self._run_task, args=(session.id, text), daemon=True)
        worker.start()

    def _send_shortcut(self, _event: tk.Event[Any]) -> str:
        self.send_message()
        return "break"

    def _send_enter(self, _event: tk.Event[Any]) -> str:
        self.send_message()
        return "break"

    def _insert_newline(self, _event: tk.Event[Any]) -> str:
        self.input_box.insert("insert", "\n")
        return "break"

    def _run_task(self, task_id: str, text: str) -> None:
        session = self._find_task(task_id)
        if session is None:
            return
        try:
            answer = session.agent.run(text)
            self.events.put(("complete", task_id, answer))
        except AgentCancelled as exc:
            self.events.put(("cancelled", task_id, str(exc)))
        except (AgentStopped, ModelError, ValueError) as exc:
            self.events.put(("error", task_id, str(exc)))
        except Exception as exc:
            self.events.put(("error", task_id, f"未预期错误: {type(exc).__name__}: {exc}"))

    def stop_task(self) -> None:
        session = self._current()
        if session and session.running:
            session.cancel_event.set()
            self._set_status("正在停止…", WARNING)

    def _request_approval(self, task_id: str, command: str, risk: RiskLevel, reason: str) -> bool:
        signal = threading.Event()
        response: dict[str, bool] = {}
        self.events.put(("approval", task_id, command, risk, reason, signal, response))
        while not signal.wait(0.1):
            if self.closing or (session := self._find_task(task_id)) is None or session.cancel_event.is_set():
                return False
        return response.get("approved", False)

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(80, self._poll_events)

    def _handle_event(self, event: tuple[str, Any]) -> None:
        kind = event[0]
        if kind == "agent_event":
            _, task_id, name, data = event
            self._handle_agent_event(task_id, name, data)
        elif kind in {"complete", "cancelled", "error"}:
            _, task_id, text = event
            session = self._find_task(task_id)
            if session is None:
                return
            session.running = False
            entry_kind = "assistant" if kind == "complete" else ("system" if kind == "cancelled" else "error")
            final_paths = tuple(
                path
                for path in dict.fromkeys(session.pending_change_paths)
                if path in session.change_tracker.changes
            )
            session.entries.append(ChatEntry(entry_kind, text, final_paths))
            session.pending_change_paths.clear()
            self._refresh_task_tree()
            self._save_sessions()
            if task_id == self.current_id:
                self._render_current()
                if kind == "complete":
                    self._set_status("任务完成", SUCCESS)
                elif kind == "cancelled":
                    self._set_status("已停止", WARNING)
                else:
                    self._set_status("需要处理", ERROR)
        elif kind == "approval":
            _, task_id, command, risk, reason, signal, response = event
            session = self._find_task(task_id)
            approved = False
            if session and not session.cancel_event.is_set():
                approved = messagebox.askyesno(
                    "命令执行确认",
                    f"任务：{session.title}\n风险：{risk.value}\n原因：{reason}\n\n{command}\n\n允许执行吗？",
                    parent=self.root,
                )
            response["approved"] = approved
            signal.set()

    def _handle_agent_event(self, task_id: str, name: str, data: dict[str, Any]) -> None:
        session = self._find_task(task_id)
        if session is None:
            return
        if name == "model_start":
            status = f"模型思考中 · {data['step']}/{data['max_steps']}"
            tone = ACCENT
        elif name == "summary_start":
            status = "正在压缩上下文"
            tone = WARNING
            session.entries.append(ChatEntry("system", "正在压缩较早的对话上下文…"))
        elif name == "tool_start":
            status = f"正在执行 {data['name']}"
            tone = TOOL_STATUS
            arguments = data.get("arguments", "")
            session.entries.append(ChatEntry("tool", f"→ {data['name']}\n{arguments[:1200]}"))
        elif name == "tool_end":
            status = "工具执行完成" if data["ok"] else "工具执行失败"
            tone = SUCCESS if data["ok"] else ERROR
            detail = data.get("error") or data.get("output") or ""
            marker = "✓" if data["ok"] else "✗"
            change_data = data.get("changes")
            change_paths = tuple(
                path
                for path in change_data.get("paths", [])
                if isinstance(path, str)
            ) if isinstance(change_data, dict) else ()
            tracking_warning = change_data.get("warning") if isinstance(change_data, dict) else None
            rendered = f"{marker} {data['name']}\n{detail[:2000]}"
            if isinstance(tracking_warning, str) and tracking_warning:
                rendered += f"\n⚠ {tracking_warning}"
            session.entries.append(
                ChatEntry("tool" if data["ok"] else "error", rendered)
            )
            for path in change_paths:
                if path not in session.pending_change_paths:
                    session.pending_change_paths.append(path)
            self._save_sessions()
        else:
            return
        if task_id == self.current_id:
            self._set_status(status, tone)
            self._render_transcript(session)

    def _select_task(self, _event: tk.Event[Any]) -> None:
        selected = self.task_tree.selection()
        if not selected:
            return
        item = selected[0]
        if item.startswith("task:"):
            task_id = item.removeprefix("task:")
            if self._find_task(task_id):
                self.current_id = task_id
                self._render_current()
        elif item.startswith("project:"):
            project_id = item.removeprefix("project:")
            project_tasks = [task for task in self.tasks if task.project_id == project_id]
            if project_tasks:
                self.current_id = project_tasks[0].id
                self._render_current()

    def _tree_item_target(self, item_id: str) -> TaskSession | ProjectSession | None:
        if item_id.startswith("task:"):
            return self._find_task(item_id.removeprefix("task:"))
        if item_id.startswith("project:"):
            return self._find_project(item_id.removeprefix("project:"))
        return None

    def _rename_tree_item(self, item_id: str, name: str) -> bool:
        target = self._tree_item_target(item_id)
        normalized = normalize_display_name(name)
        if target is None or not normalized:
            return False
        target.title = normalized
        if isinstance(target, TaskSession):
            target.title_is_custom = True
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()
        return True

    def _prompt_rename_tree_item(self, item_id: str) -> None:
        target = self._tree_item_target(item_id)
        if target is None:
            return
        is_project = isinstance(target, ProjectSession)
        dialog = RenameDialog(
            self.root,
            "重命名项目" if is_project else "重命名对话",
            "项目名称" if is_project else "对话名称",
            target.title,
        )
        self.root.wait_window(dialog)
        if dialog.result is not None:
            self._rename_tree_item(item_id, dialog.result)

    def _show_item_menu(self, item_id: str, x_root: int, y_root: int) -> None:
        target = self._tree_item_target(item_id)
        if target is None:
            return
        menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=SURFACE,
            fg=TEXT,
            activebackground=BLUSH,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            font=(UI_FONT, 10),
        )
        menu.add_command(label="重命名", command=lambda target_id=item_id: self._prompt_rename_tree_item(target_id))
        menu.add_separator()
        menu.add_command(
            label="移除项目" if isinstance(target, ProjectSession) else "删除对话",
            command=lambda target_id=item_id: self.delete_task(target_id),
        )
        self._post_transient_menu(menu, x_root, y_root)

    def _post_transient_menu(self, menu: tk.Menu, x_root: int, y_root: int) -> None:
        active_menu = getattr(self, "_active_menu", None)
        if active_menu is not None:
            active_menu.destroy()
        self._active_menu = menu
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _show_current_project_menu(self) -> None:
        project = self._current_project()
        if project is None:
            return
        project_id = project.id
        menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=SURFACE,
            fg=TEXT,
            activebackground=BLUSH,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            font=(UI_FONT, 10),
        )
        menu.add_command(label="重命名", command=lambda target_id=project_id: self._prompt_rename_tree_item(f"project:{target_id}"))
        self._post_transient_menu(
            menu,
            self.project_menu_button.winfo_rootx(),
            self.project_menu_button.winfo_rooty() + self.project_menu_button.winfo_height(),
        )

    def _show_composer_menu(self) -> None:
        session = self._current()
        if session is None:
            return
        menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=SURFACE,
            fg=TEXT,
            activebackground=BLUSH,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            font=(UI_FONT, 10),
        )
        menu.add_command(
            label="更换工作目录…" if self._find_project(session.project_id) else "选择工作目录…",
            command=self.choose_workspace_for_current,
        )
        self._post_transient_menu(
            menu,
            self.composer_menu_button.winfo_rootx(),
            self.composer_menu_button.winfo_rooty() + self.composer_menu_button.winfo_height(),
        )

    def _show_tree_actions(self, event: tk.Event[Any]) -> str | None:
        item_id = self.task_tree.identify_row(event.y)
        if self._tree_item_target(item_id) is None:
            self._hide_tree_actions()
            return None
        if getattr(event, "num", None) == 3:
            self._hide_tree_actions()
            self._show_item_menu(item_id, event.x_root, event.y_root)
            return "break"
        bbox = self.task_tree.bbox(item_id)
        if not bbox:
            self._hide_tree_actions()
            return None
        x, y, width, height = bbox
        self._hover_item = item_id
        self.tree_actions.configure(
            command=lambda target_id=item_id: self._show_item_menu(
                target_id,
                self.tree_actions.winfo_rootx(),
                self.tree_actions.winfo_rooty() + self.tree_actions.winfo_height(),
            )
        )
        self.tree_actions.place(
            x=self.task_tree.winfo_x() + x + width - 31,
            y=self.task_tree.winfo_y() + y + 4,
            width=27,
            height=max(1, height - 8),
        )
        return None

    def _hide_tree_actions(self, event: tk.Event[Any] | None = None) -> None:
        if event is not None:
            hovered = self.root.winfo_containing(event.x_root, event.y_root)
            if hovered in (self.task_tree, self.tree_actions):
                return
        self._hover_item = None
        self.tree_actions.place_forget()

    def _refresh_task_tree(self) -> None:
        if hasattr(self, "tree_actions"):
            self._hide_tree_actions()
        self.task_tree.delete(*self.task_tree.get_children())
        conversations_item = "section:conversations"
        projects_item = "section:projects"
        self.task_tree.insert("", "end", iid=conversations_item, text="对话", open=True, tags=("section",))
        for task in (candidate for candidate in self.tasks if candidate.project_id is None):
            marker = "●  " if task.running else "·  "
            tags = ("running",) if task.running else ("task",)
            self.task_tree.insert(conversations_item, "end", iid=f"task:{task.id}", text=marker + task.title, tags=tags)
        self.task_tree.insert("", "end", iid=projects_item, text="项目", open=True, tags=("section",))
        for project in self.projects:
            project_item = f"project:{project.id}"
            self.task_tree.insert(projects_item, "end", iid=project_item, text=project.title.upper(), open=True, tags=("project",))
            for task in (candidate for candidate in self.tasks if candidate.project_id == project.id):
                marker = "●  " if task.running else "·  "
                tags = ("running",) if task.running else ("task",)
                self.task_tree.insert(project_item, "end", iid=f"task:{task.id}", text=marker + task.title, tags=tags)
        if self.current_id and self.task_tree.exists(f"task:{self.current_id}"):
            self.task_tree.selection_set(f"task:{self.current_id}")
            self.task_tree.see(f"task:{self.current_id}")

    def _render_current(self) -> None:
        session = self._current()
        if session is None:
            return
        project = self._find_project(session.project_id)
        self.title_label.configure(text=session.title)
        self._set_status("运行中" if session.running else "就绪", ACCENT if session.running else MUTED)
        self.send_button.configure(state="disabled" if session.running else "normal")
        self.stop_button.configure(state="normal" if session.running else "disabled")
        self.input_box.configure(state="disabled" if session.running else "normal")
        if project:
            self.project_label.configure(text=f"{project.title.upper()}  /  对话")
            if not self.project_menu_button.winfo_ismapped():
                self.project_menu_button.pack(side="left", padx=(4, 0))
            self.workspace_label.configure(text=f"工作目录  {project.path}")
        else:
            self.project_label.configure(text="未选择工作目录 / 对话")
            self.project_menu_button.pack_forget()
            self.workspace_label.configure(text="尚未选择工作目录")
        self._render_transcript(session)
        self._sync_review_pane(session)

    def _show_review_container(self) -> None:
        if not hasattr(self, "content_split") or not hasattr(self, "review_container"):
            return
        panes = {str(pane) for pane in self.content_split.panes()}
        if str(self.review_container) not in panes:
            self.content_split.add(self.review_container, minsize=320, width=480, stretch="always")

    def _hide_review_container(self) -> None:
        if not hasattr(self, "content_split") or not hasattr(self, "review_container"):
            return
        panes = {str(pane) for pane in self.content_split.panes()}
        if str(self.review_container) in panes:
            self.content_split.forget(self.review_container)

    def _open_change(self, path: str) -> None:
        session = self._current()
        if session is None:
            return
        change = session.change_tracker.changes.get(path)
        if change is None:
            return
        session.review_path = path
        self.review_pane.show_change(change)
        self._show_review_container()
        self._save_sessions()

    def _close_review(self) -> None:
        session = self._current()
        if session is not None:
            session.review_path = None
        if hasattr(self, "review_pane"):
            self.review_pane.clear()
        self._hide_review_container()
        self._save_sessions()

    def _sync_review_pane(self, session: TaskSession) -> None:
        if not hasattr(self, "review_pane"):
            return
        change = session.change_tracker.changes.get(session.review_path or "")
        if change is None:
            if session.review_path is not None:
                session.review_path = None
            self.review_pane.clear()
            self._hide_review_container()
            return
        self.review_pane.show_change(change)
        self._show_review_container()

    def _render_transcript(self, session: TaskSession) -> None:
        for card in getattr(self, "_change_cards", []):
            card.destroy()
        self._change_cards = []
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        if not session.entries:
            title, body, suggestions = EMPTY_STATE
            self.transcript.insert("end", "◕‿◕\n", "empty_face")
            self.transcript.insert("end", title + "\n", "empty_title")
            self.transcript.insert("end", body + "\n", "empty_body")
            for suggestion in suggestions:
                self.transcript.insert("end", f"✦  {suggestion}\n", "empty_hint")
            if session.project_id is None:
                self.transcript.insert("end", "＋ 可启用本地文件操作\n", "empty_hint")
        for entry in session.entries:
            if entry.kind == "user":
                self.transcript.insert("end", "你\n", "user_label")
                self.transcript.insert("end", entry.text + "\n", "user")
            elif entry.kind == "assistant":
                self.transcript.insert("end", ASSISTANT_LABEL + "\n", "assistant_label")
                self.transcript.insert("end", entry.text + "\n", "assistant")
            else:
                self.transcript.insert("end", entry.text + "\n", entry.kind)
            visible_paths = [
                path
                for path in dict.fromkeys(entry.change_paths)
                if path in session.change_tracker.changes
            ]
            if visible_paths:
                self.transcript.insert(
                    "end",
                    f"本轮改动 · {len(visible_paths)} 个文件\n",
                    "changes_header",
                )
            for path in visible_paths:
                change = session.change_tracker.changes.get(path)
                assert change is not None
                card = FileChangeCard(
                    self.transcript,
                    change,
                    lambda selected=path: self._open_change(selected),
                    DIFF_PALETTE,
                )
                self._change_cards.append(card)
                self.transcript.window_create("end", window=card, padx=16, pady=4, stretch=True)
                self.transcript.insert("end", "\n")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _set_status(self, text: str, tone: str) -> None:
        self.status_label.configure(text=f"● {text}", fg=tone)
        self.activity_bar.configure(bg=tone)

    def open_settings(self) -> None:
        if any(task.running for task in self.tasks):
            messagebox.showwarning("任务运行中", "停止所有运行中的任务后才能更改设置。", parent=self.root)
            return
        dialog = ConfigDialog(self.root, self.settings)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self.config, self.settings = dialog.result
        try:
            self.settings.save(self.settings_root)
        except OSError as exc:
            messagebox.showwarning("保存失败", f"本地设置无法保存：{exc}", parent=self.root)
        for session in self.tasks:
            project = self._find_project(session.project_id)
            self._retarget_agent(session, project)
        self.status_label.configure(text="设置已更新", fg=SUCCESS)
        self._save_sessions()

    def _current(self) -> TaskSession | None:
        return self._find_task(self.current_id) if self.current_id else None

    def _find_task(self, task_id: str | None) -> TaskSession | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def _find_project(self, project_id: str | None) -> ProjectSession | None:
        return next((project for project in self.projects if project.id == project_id), None)

    def _current_project(self) -> ProjectSession | None:
        session = self._current()
        if session:
            return self._find_project(session.project_id)
        selected = self.task_tree.selection()
        if selected and selected[0].startswith("project:"):
            return self._find_project(selected[0].removeprefix("project:"))
        return self.projects[0] if self.projects else None

    def _load_sessions(self) -> None:
        state = self.store.load()
        projects_by_id: dict[str, ProjectSession] = {}
        for raw_project in state.get("projects", []):
            if not isinstance(raw_project, dict):
                continue
            raw_path = raw_project.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            try:
                path = Path(raw_path).expanduser().resolve()
            except (KeyError, OSError):
                continue
            if not path.is_dir():
                continue
            project_id = str(raw_project.get("id") or uuid.uuid4().hex)
            project = ProjectSession(
                id=project_id,
                path=path,
                title=str(raw_project.get("title") or path.name or path),
            )
            self.projects.append(project)
            projects_by_id[project.id] = project

        for raw_task in state.get("tasks", []):
            if not isinstance(raw_task, dict):
                continue
            task_id = str(raw_task.get("id") or uuid.uuid4().hex)
            raw_project_id = raw_task.get("project_id")
            project = projects_by_id.get(raw_project_id) if isinstance(raw_project_id, str) else None
            cancel_event = threading.Event()
            change_tracker = ConversationChangeTracker(project.path if project else None)
            change_tracker.load_serialized(raw_task.get("file_changes", []))
            agent = self._make_agent(
                task_id,
                cancel_event,
                project.path if project else None,
                change_tracker,
            )
            history = raw_task.get("history")
            if isinstance(history, list) and history and all(isinstance(message, dict) for message in history):
                agent.history = [agent.history[0], *history[1:]]
            raw_entries = raw_task.get("entries", [])
            entries = [
                ChatEntry(
                    str(entry.get("kind", "system")),
                    str(entry.get("text", "")),
                    tuple(
                        path
                        for path in entry.get("change_paths", [])
                        if isinstance(path, str)
                    ) if isinstance(entry.get("change_paths", []), list) else (),
                )
                for entry in raw_entries
                if isinstance(entry, dict)
            ] if isinstance(raw_entries, list) else []
            unfinished_paths = collapse_change_entries(entries)
            raw_pending_paths = raw_task.get("pending_change_paths", [])
            if isinstance(raw_pending_paths, list):
                for path in raw_pending_paths:
                    if isinstance(path, str) and path not in unfinished_paths:
                        unfinished_paths.append(path)
            recovered_paths = tuple(
                path for path in unfinished_paths if path in change_tracker.changes
            )
            if recovered_paths:
                entries.append(
                    ChatEntry(
                        "system",
                        "上次任务在结束前中断，以下为已经记录的文件改动。",
                        recovered_paths,
                    )
                )
            raw_review_path = raw_task.get("review_path")
            review_path = (
                raw_review_path
                if isinstance(raw_review_path, str) and raw_review_path in change_tracker.changes
                else None
            )
            self.tasks.append(
                TaskSession(
                    id=task_id,
                    project_id=project.id if project else None,
                    title=str(raw_task.get("title") or "新对话"),
                    agent=agent,
                    cancel_event=cancel_event,
                    change_tracker=change_tracker,
                    entries=entries,
                    title_is_custom=bool(raw_task.get("title_is_custom", False)),
                    review_path=review_path,
                    pending_change_paths=[],
                )
            )

        current_id = state.get("current_id")
        if isinstance(current_id, str) and self._find_task(current_id):
            self.current_id = current_id

    def _save_sessions(self) -> None:
        payload = {
            "version": 3,
            "current_id": self.current_id,
            "projects": [
                {
                    "id": project.id,
                    "title": project.title,
                    "path": str(project.path),
                }
                for project in self.projects
            ],
            "tasks": [
                {
                    "id": task.id,
                    "project_id": task.project_id,
                    "title": task.title,
                    "title_is_custom": task.title_is_custom,
                    "entries": [
                        {
                            "kind": entry.kind,
                            "text": entry.text,
                            "change_paths": list(entry.change_paths),
                        }
                        for entry in task.entries
                    ],
                    "history": task.agent.history,
                    "file_changes": task.change_tracker.serialize(),
                    "review_path": task.review_path,
                    "pending_change_paths": list(task.pending_change_paths),
                }
                for task in self.tasks
            ],
        }
        try:
            self.store.save(payload)
        except OSError:
            if not self.closing:
                self.status_label.configure(text="本地会话保存失败", fg=ERROR)

    def _close(self) -> None:
        running = [task for task in self.tasks if task.running]
        if running and not messagebox.askyesno("退出", "仍有任务运行。停止任务并退出吗？", parent=self.root):
            return
        self.closing = True
        for task in running:
            task.cancel_event.set()
        self._save_sessions()
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent", description="本地编程智能体桌面应用")
    parser.add_argument("--workspace", type=Path, help="初始工作区")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings_root = Path.cwd().resolve()
    settings = LocalSettings.load(settings_root)
    if args.workspace:
        settings.workspace = str(args.workspace.expanduser().resolve())

    root = tk.Tk()
    for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(font_name).configure(family=UI_FONT, size=10)
    root.withdraw()
    config: Config | None = None
    if settings.is_complete:
        workspace = Path(settings.workspace).expanduser()
        if not workspace.is_dir():
            workspace = settings_root
        try:
            config = Config.from_values(
                api_key=settings.api_key,
                model=settings.model,
                base_url=settings.base_url,
                workspace=workspace,
                context_tokens=settings.context_tokens,
                max_steps=settings.max_steps,
                approval_mode=settings.approval_mode,
            )
        except ConfigError:
            config = None
    if config is None:
        dialog = ConfigDialog(root, settings, required=True)
        root.wait_window(dialog)
        if dialog.result is None:
            try:
                root.destroy()
            except tk.TclError:
                pass
            return 1
        config, settings = dialog.result
        try:
            settings.save(settings_root)
        except OSError as exc:
            messagebox.showwarning("保存失败", f"本地设置无法保存：{exc}", parent=root)
    root.deiconify()
    CodingAgentApp(root, config, settings, settings_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
