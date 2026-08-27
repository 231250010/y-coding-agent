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
from .config import Config, ConfigError
from .context import ContextManager
from .local_settings import LocalSettings
from .model import ModelError, OpenAIChatModel
from .safety import RiskLevel
from .session_store import SessionStore
from .tools import ToolRegistry


BG = "#111318"
PANEL = "#181b22"
PANEL_ALT = "#20242d"
BORDER = "#303642"
TEXT = "#e7e9ee"
MUTED = "#969daa"
ACCENT = "#4f8cff"
USER_BG = "#26344f"
TOOL_BG = "#202a25"
ERROR = "#ff7b72"
SUCCESS = "#66c98f"
UI_FONT = "Microsoft YaHei UI"


@dataclass(slots=True)
class ChatEntry:
    kind: str
    text: str


@dataclass(slots=True)
class TaskSession:
    id: str
    project_id: str
    title: str
    agent: CodingAgent
    cancel_event: threading.Event
    entries: list[ChatEntry] = field(default_factory=list)
    running: bool = False


@dataclass(slots=True)
class ProjectSession:
    id: str
    path: Path
    title: str


class ConfigDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, settings: LocalSettings, *, required: bool = False) -> None:
        super().__init__(parent)
        self.title("模型连接设置")
        self.configure(bg=PANEL)
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
        body = tk.Frame(self, bg=PANEL, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="模型连接", bg=PANEL, fg=TEXT, font=(UI_FONT, 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 16)
        )
        rows = [
            ("API Key", "api_key", True),
            ("模型", "model", False),
            ("Base URL", "base_url", False),
            ("上下文预算", "context_tokens", False),
            ("最大步骤", "max_steps", False),
        ]
        for index, (label, key, secret) in enumerate(rows, start=1):
            tk.Label(body, text=label, bg=PANEL, fg=MUTED, anchor="w", width=12).grid(
                row=index, column=0, sticky="w", pady=6
            )
            entry = tk.Entry(
                body,
                textvariable=self.variables[key],
                show="•" if secret else "",
                width=44,
                bg=PANEL_ALT,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=ACCENT,
            )
            entry.grid(row=index, column=1, sticky="ew", ipady=6, pady=6)

        option_row = len(rows) + 1
        tk.Label(body, text="命令审批", bg=PANEL, fg=MUTED, anchor="w").grid(
            row=option_row, column=0, sticky="w", pady=6
        )
        approval = ttk.Combobox(
            body,
            textvariable=self.variables["approval_mode"],
            values=("ask", "always"),
            state="readonly",
            width=41,
        )
        approval.grid(row=option_row, column=1, sticky="ew", pady=6)
        tk.Checkbutton(
            body,
            text="将 API Key 保存在本地忽略配置中",
            variable=self.variables["remember_key"],
            bg=PANEL,
            fg=MUTED,
            activebackground=PANEL,
            activeforeground=TEXT,
            selectcolor=PANEL_ALT,
        ).grid(row=option_row + 1, column=1, sticky="w", pady=(6, 2))
        tk.Label(
            body,
            text="配置文件位于 .coding-agent/config.json，已被 Git 忽略。",
            bg=PANEL,
            fg=MUTED,
            font=(UI_FONT, 9),
        ).grid(row=option_row + 2, column=1, sticky="w")

        buttons = tk.Frame(body, bg=PANEL)
        buttons.grid(row=option_row + 3, column=0, columnspan=3, sticky="e", pady=(20, 0))
        if not self.required:
            tk.Button(
                buttons, text="取消", command=self._cancel, bg=PANEL_ALT, fg=TEXT, relief="flat", padx=18, pady=7
            ).pack(side="left", padx=(0, 8))
        tk.Button(
            buttons,
            text="保存并连接",
            command=self._save,
            bg=ACCENT,
            fg="white",
            activebackground="#3975db",
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

        self._configure_window()
        self._build_layout()
        self._load_sessions()
        if not self.projects:
            self._add_project(self.config.workspace)
        elif self.tasks:
            self.current_id = self.tasks[0].id
        else:
            self.new_task(self.projects[0].id)
        self._refresh_task_tree()
        self._render_current()
        self.root.after(80, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_window(self) -> None:
        self.root.title("Coding Agent")
        self.root.geometry("1180x760")
        self.root.minsize(900, 600)
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=PANEL_ALT, background=PANEL_ALT, foreground=TEXT)

    def _build_layout(self) -> None:
        split = tk.PanedWindow(self.root, orient="horizontal", bg=BORDER, sashwidth=2, bd=0)
        split.pack(fill="both", expand=True)

        sidebar = tk.Frame(split, bg=PANEL, width=275)
        content = tk.Frame(split, bg=BG)
        split.add(sidebar, minsize=230, width=275)
        split.add(content, minsize=620)

        side_header = tk.Frame(sidebar, bg=PANEL, padx=16, pady=16)
        side_header.pack(fill="x")
        tk.Label(side_header, text="项目", bg=PANEL, fg=TEXT, font=(UI_FONT, 17, "bold")).pack(side="left")
        tk.Button(
            side_header,
            text="＋ 项目",
            command=self.choose_project,
            bg=ACCENT,
            fg="white",
            activebackground="#3975db",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=5,
        ).pack(side="right")

        task_actions = tk.Frame(sidebar, bg=PANEL, padx=12)
        task_actions.pack(fill="x", pady=(0, 8))
        self._flat_button(task_actions, "＋ 新对话", self.new_task).pack(fill="x")

        tree_style = ttk.Style(self.root)
        tree_style.configure(
            "Tasks.Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            borderwidth=0,
            rowheight=31,
            font=(UI_FONT, 10),
        )
        tree_style.map("Tasks.Treeview", background=[("selected", PANEL_ALT)], foreground=[("selected", TEXT)])
        self.task_tree = ttk.Treeview(
            sidebar,
            style="Tasks.Treeview",
            show="tree",
            selectmode="browse",
        )
        self.task_tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.task_tree.bind("<<TreeviewSelect>>", self._select_task)

        side_footer = tk.Frame(sidebar, bg=PANEL, padx=12, pady=12)
        side_footer.pack(fill="x")
        self._flat_button(side_footer, "删除", self.delete_task).pack(side="left")
        self._flat_button(side_footer, "设置", self.open_settings).pack(side="right")

        header = tk.Frame(content, bg=BG, padx=24, pady=14)
        header.pack(fill="x")
        self.title_label = tk.Label(header, text="", bg=BG, fg=TEXT, font=(UI_FONT, 16, "bold"))
        self.title_label.pack(side="left")
        self.status_label = tk.Label(header, text="就绪", bg=BG, fg=MUTED, font=(UI_FONT, 10))
        self.status_label.pack(side="right", padx=(12, 0))
        self.stop_button = tk.Button(
            header,
            text="停止",
            command=self.stop_task,
            state="disabled",
            bg="#40252a",
            fg="#ff9b98",
            disabledforeground="#70585c",
            activebackground="#553036",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=5,
        )
        self.stop_button.pack(side="right")

        transcript_frame = tk.Frame(content, bg=BG, padx=24)
        transcript_frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(transcript_frame, relief="flat")
        scrollbar.pack(side="right", fill="y")
        self.transcript = tk.Text(
            transcript_frame,
            wrap="word",
            state="disabled",
            bg=BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=0,
            font=(UI_FONT, 11),
            padx=10,
            pady=10,
            yscrollcommand=scrollbar.set,
        )
        self.transcript.pack(fill="both", expand=True)
        scrollbar.configure(command=self.transcript.yview)
        self._configure_transcript_tags()

        composer = tk.Frame(content, bg=BG, padx=24, pady=18)
        composer.pack(fill="x")
        input_border = tk.Frame(composer, bg=BORDER, padx=1, pady=1)
        input_border.pack(fill="x")
        input_inner = tk.Frame(input_border, bg=PANEL_ALT, padx=10, pady=10)
        input_inner.pack(fill="x")
        self.input_box = tk.Text(
            input_inner,
            height=4,
            wrap="word",
            bg=PANEL_ALT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=0,
            font=(UI_FONT, 11),
            undo=True,
        )
        self.input_box.pack(fill="x")
        self.input_box.bind("<Control-Return>", self._send_shortcut)
        composer_actions = tk.Frame(input_inner, bg=PANEL_ALT)
        composer_actions.pack(fill="x", pady=(8, 0))
        self.workspace_label = tk.Label(
            composer_actions,
            text=str(self.config.workspace),
            bg=PANEL_ALT,
            fg=MUTED,
            font=(UI_FONT, 9),
        )
        self.workspace_label.pack(side="left")
        self.send_button = tk.Button(
            composer_actions,
            text="发送  Ctrl+Enter",
            command=self.send_message,
            bg=ACCENT,
            fg="white",
            activebackground="#3975db",
            activeforeground="white",
            relief="flat",
            padx=14,
            pady=6,
        )
        self.send_button.pack(side="right")

    @staticmethod
    def _flat_button(parent: tk.Misc, text: str, command: Any) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=PANEL_ALT,
            fg=MUTED,
            activebackground=BORDER,
            activeforeground=TEXT,
            relief="flat",
            padx=10,
            pady=5,
        )

    def _configure_transcript_tags(self) -> None:
        self.transcript.tag_configure("user_label", foreground="#8fb5ff", font=(UI_FONT, 10, "bold"), spacing1=12)
        self.transcript.tag_configure("user", foreground=TEXT, background=USER_BG, lmargin1=14, lmargin2=14, rmargin=14, spacing3=12)
        self.transcript.tag_configure("assistant_label", foreground=SUCCESS, font=(UI_FONT, 10, "bold"), spacing1=12)
        self.transcript.tag_configure("assistant", foreground=TEXT, lmargin1=14, lmargin2=14, rmargin=14, spacing3=12)
        self.transcript.tag_configure("tool", foreground="#b8c8bc", background=TOOL_BG, lmargin1=14, lmargin2=14, rmargin=14, spacing1=5, spacing3=5)
        self.transcript.tag_configure("error", foreground=ERROR, lmargin1=14, lmargin2=14, rmargin=14, spacing1=5, spacing3=5)
        self.transcript.tag_configure("system", foreground=MUTED, lmargin1=14, lmargin2=14, rmargin=14, spacing1=5, spacing3=5)

    def _make_agent(self, task_id: str, cancel_event: threading.Event, workspace: Path) -> CodingAgent:
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
        )
        return CodingAgent(
            model,
            tools,
            ContextManager(self.config.context_tokens),
            max_steps=self.config.max_steps,
            on_event=lambda name, data: self.events.put(("agent_event", task_id, name, data)),
            is_cancelled=cancel_event.is_set,
        )

    def choose_project(self) -> None:
        current_project = self._current_project()
        initial = current_project.path if current_project else Path(self.settings.workspace or self.config.workspace)
        selected = filedialog.askdirectory(parent=self.root, initialdir=str(initial), title="选择项目目录")
        if selected:
            self._add_project(Path(selected))

    def _add_project(self, path: Path) -> ProjectSession:
        resolved = path.expanduser().resolve()
        existing = next((project for project in self.projects if project.path == resolved), None)
        if existing:
            tasks = [task for task in self.tasks if task.project_id == existing.id]
            if tasks:
                self.current_id = tasks[0].id
            else:
                self.new_task(existing.id)
            self._refresh_task_tree()
            self._render_current()
            return existing
        project = ProjectSession(id=uuid.uuid4().hex, path=resolved, title=resolved.name or str(resolved))
        self.projects.append(project)
        self.settings.workspace = str(resolved)
        self.new_task(project.id)
        self._save_sessions()
        return project

    def new_task(self, project_id: str | None = None) -> None:
        project = self._find_project(project_id) if project_id else self._current_project()
        if project is None:
            self.choose_project()
            return
        task_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        project_tasks = [task for task in self.tasks if task.project_id == project.id]
        session = TaskSession(
            id=task_id,
            project_id=project.id,
            title=f"新对话 {len(project_tasks) + 1}",
            agent=self._make_agent(task_id, cancel_event, project.path),
            cancel_event=cancel_event,
        )
        self.tasks.append(session)
        self.current_id = task_id
        self._refresh_task_tree()
        self._render_current()
        self._save_sessions()
        self.input_box.focus_set()

    def delete_task(self) -> None:
        selected = self.task_tree.selection()
        if not selected:
            return
        item = selected[0]
        if item.startswith("project:"):
            project = self._find_project(item.removeprefix("project:"))
            if project is None:
                return
            project_tasks = [task for task in self.tasks if task.project_id == project.id]
            if any(task.running for task in project_tasks):
                messagebox.showwarning("项目运行中", "请先停止该项目中运行的对话。", parent=self.root)
                return
            if not messagebox.askyesno(
                "移除项目", f"从侧栏移除“{project.title}”及其本地对话记录吗？\n不会删除项目目录中的文件。", parent=self.root
            ):
                return
            removed_ids = {task.id for task in project_tasks}
            self.tasks = [task for task in self.tasks if task.id not in removed_ids]
            self.projects = [candidate for candidate in self.projects if candidate.id != project.id]
            self.current_id = self.tasks[0].id if self.tasks else None
            if not self.projects:
                self._add_project(self.config.workspace)
            self._refresh_task_tree()
            self._render_current()
            self._save_sessions()
            return
        session = self._current()
        if session is None:
            return
        if session.running:
            messagebox.showwarning("任务运行中", "请先停止任务，再将其删除。", parent=self.root)
            return
        if not messagebox.askyesno("删除任务", f"确定删除“{session.title}”吗？", parent=self.root):
            return
        self.tasks = [task for task in self.tasks if task.id != session.id]
        siblings = [task for task in self.tasks if task.project_id == session.project_id]
        if not siblings:
            self.current_id = None
            self.new_task(session.project_id)
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
        if session.title.startswith("新对话"):
            session.title = text.replace("\n", " ")[:32]
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
            self.status_label.configure(text="正在停止…", fg="#ffbc7a")

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
            session.entries.append(ChatEntry(entry_kind, text))
            self._refresh_task_tree()
            self._save_sessions()
            if task_id == self.current_id:
                self._render_current()
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
        elif name == "summary_start":
            status = "正在压缩上下文"
            session.entries.append(ChatEntry("system", "正在压缩较早的对话上下文…"))
        elif name == "tool_start":
            status = f"正在执行 {data['name']}"
            arguments = data.get("arguments", "")
            session.entries.append(ChatEntry("tool", f"→ {data['name']}\n{arguments[:1200]}"))
        elif name == "tool_end":
            status = "工具执行完成" if data["ok"] else "工具执行失败"
            detail = data.get("error") or data.get("output") or ""
            marker = "✓" if data["ok"] else "✗"
            session.entries.append(ChatEntry("tool" if data["ok"] else "error", f"{marker} {data['name']}\n{detail[:2000]}"))
        else:
            return
        if task_id == self.current_id:
            self.status_label.configure(text=status, fg=MUTED)
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

    def _refresh_task_tree(self) -> None:
        self.task_tree.delete(*self.task_tree.get_children())
        for project in self.projects:
            project_item = f"project:{project.id}"
            self.task_tree.insert("", "end", iid=project_item, text=project.title, open=True)
            for task in (candidate for candidate in self.tasks if candidate.project_id == project.id):
                marker = "● " if task.running else "  "
                self.task_tree.insert(project_item, "end", iid=f"task:{task.id}", text=marker + task.title)
        if self.current_id and self.task_tree.exists(f"task:{self.current_id}"):
            self.task_tree.selection_set(f"task:{self.current_id}")
            self.task_tree.see(f"task:{self.current_id}")

    def _render_current(self) -> None:
        session = self._current()
        if session is None:
            return
        project = self._find_project(session.project_id)
        self.title_label.configure(text=session.title)
        self.status_label.configure(text="运行中" if session.running else "就绪", fg=MUTED)
        self.send_button.configure(state="disabled" if session.running else "normal")
        self.stop_button.configure(state="normal" if session.running else "disabled")
        self.input_box.configure(state="disabled" if session.running else "normal")
        if project:
            self.workspace_label.configure(text=f"{project.title}  ·  {project.path}")
        self._render_transcript(session)

    def _render_transcript(self, session: TaskSession) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        if not session.entries:
            self.transcript.insert("end", "描述你希望在当前工作区完成的编程任务。\n", "system")
        for entry in session.entries:
            if entry.kind == "user":
                self.transcript.insert("end", "你\n", "user_label")
                self.transcript.insert("end", entry.text + "\n", "user")
            elif entry.kind == "assistant":
                self.transcript.insert("end", "Coding Agent\n", "assistant_label")
                self.transcript.insert("end", entry.text + "\n", "assistant")
            else:
                self.transcript.insert("end", entry.text + "\n", entry.kind)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

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
            history = list(session.agent.history)
            project = self._find_project(session.project_id)
            if project is None:
                continue
            session.agent = self._make_agent(session.id, session.cancel_event, project.path)
            session.agent.history = history
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
        for raw_project in state.get("projects", []):
            if not isinstance(raw_project, dict):
                continue
            try:
                path = Path(str(raw_project["path"])).expanduser().resolve()
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
            raw_tasks = raw_project.get("tasks", [])
            if not isinstance(raw_tasks, list):
                continue
            for raw_task in raw_tasks:
                if not isinstance(raw_task, dict):
                    continue
                task_id = str(raw_task.get("id") or uuid.uuid4().hex)
                cancel_event = threading.Event()
                agent = self._make_agent(task_id, cancel_event, path)
                history = raw_task.get("history")
                if isinstance(history, list) and history and all(isinstance(message, dict) for message in history):
                    agent.history = history
                raw_entries = raw_task.get("entries", [])
                entries = [
                    ChatEntry(str(entry.get("kind", "system")), str(entry.get("text", "")))
                    for entry in raw_entries
                    if isinstance(entry, dict)
                ] if isinstance(raw_entries, list) else []
                self.tasks.append(
                    TaskSession(
                        id=task_id,
                        project_id=project_id,
                        title=str(raw_task.get("title") or "新对话"),
                        agent=agent,
                        cancel_event=cancel_event,
                        entries=entries,
                    )
                )

    def _save_sessions(self) -> None:
        payload = {
            "version": 1,
            "projects": [
                {
                    "id": project.id,
                    "title": project.title,
                    "path": str(project.path),
                    "tasks": [
                        {
                            "id": task.id,
                            "title": task.title,
                            "entries": [{"kind": entry.kind, "text": entry.text} for entry in task.entries],
                            "history": task.agent.history,
                        }
                        for task in self.tasks
                        if task.project_id == project.id
                    ],
                }
                for project in self.projects
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
