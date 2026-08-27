from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent import gui
from coding_agent.changes import ConversationChangeTracker
from coding_agent.gui import CodingAgentApp, ProjectSession, TaskSession, normalize_display_name


class FakeAgent:
    def __init__(self, workspace: Path | None) -> None:
        self.workspace = workspace
        self.history = [{"role": "system", "content": "projectless" if workspace is None else "project"}]


class RecordingTree:
    def __init__(self) -> None:
        self.items: dict[str, tuple[str, dict[str, object]]] = {}

    def get_children(self) -> tuple[str, ...]:
        return tuple(self.items)

    def delete(self, *item_ids: str) -> None:
        for item_id in item_ids:
            self.items.pop(item_id, None)

    def insert(self, parent: str, _index: str, *, iid: str, **options: object) -> None:
        self.items[iid] = (parent, options)

    def exists(self, item_id: str) -> bool:
        return item_id in self.items

    def selection_set(self, _item_id: str) -> None:
        pass

    def see(self, _item_id: str) -> None:
        pass


class RecordingText:
    def __init__(self) -> None:
        self.insertions: list[tuple[str, str]] = []

    def insert(self, index: str, text: str) -> None:
        self.insertions.append((index, text))


class RecordingWidget:
    def __init__(self) -> None:
        self.configurations: list[dict[str, object]] = []
        self.mapped = False

    def configure(self, **options: object) -> None:
        self.configurations.append(options)

    def pack(self, **_options: object) -> None:
        self.mapped = True

    def pack_forget(self) -> None:
        self.mapped = False

    def winfo_ismapped(self) -> bool:
        return self.mapped


class RecordingTranscript(RecordingWidget):
    def __init__(self) -> None:
        super().__init__()
        self.insertions: list[tuple[str, str, str]] = []

    def delete(self, _start: str, _end: str) -> None:
        pass

    def insert(self, index: str, text: str, tag: str) -> None:
        self.insertions.append((index, text, tag))

    def see(self, _index: str) -> None:
        pass


def make_logic_only_app() -> CodingAgentApp:
    app = CodingAgentApp.__new__(CodingAgentApp)
    app.projects = []
    app.tasks = []
    app.current_id = None
    app.events = queue.Queue()
    app.settings = SimpleNamespace(workspace="")
    app.closing = False
    app.input_box = SimpleNamespace(focus_set=lambda: None)
    app.task_tree = SimpleNamespace(selection=lambda: ())
    app._make_agent = lambda _task_id, _cancel_event, workspace, _tracker=None: FakeAgent(workspace)
    app._refresh_task_tree = lambda: None
    app._render_current = lambda: None
    app._save_sessions = lambda: None
    return app


def make_app_with_projectless_task() -> tuple[CodingAgentApp, TaskSession]:
    app = make_logic_only_app()
    task = TaskSession(
        id="task-1",
        project_id=None,
        title="Chat",
        agent=FakeAgent(None),
        cancel_event=threading.Event(),
    )
    app.tasks.append(task)
    app.current_id = task.id
    return app, task


def make_app_with_two_tasks() -> tuple[CodingAgentApp, TaskSession, TaskSession]:
    app, first = make_app_with_projectless_task()
    second = TaskSession(
        id="task-2",
        project_id=None,
        title="Second",
        agent=FakeAgent(None),
        cancel_event=threading.Event(),
    )
    app.tasks.append(second)
    return app, first, second


def make_app_with_two_projects() -> tuple[CodingAgentApp, ProjectSession, ProjectSession, TaskSession, TaskSession]:
    app = make_logic_only_app()
    first_project = ProjectSession("p1", Path("first"), "First")
    second_project = ProjectSession("p2", Path("second"), "Second")
    first_task = TaskSession(
        id="task-1",
        project_id=first_project.id,
        title="First task",
        agent=FakeAgent(first_project.path),
        cancel_event=threading.Event(),
    )
    second_task = TaskSession(
        id="task-2",
        project_id=second_project.id,
        title="Second task",
        agent=FakeAgent(second_project.path),
        cancel_event=threading.Event(),
    )
    app.projects.extend((first_project, second_project))
    app.tasks.extend((first_task, second_task))
    app.current_id = first_task.id
    return app, first_project, second_project, first_task, second_task


def make_app_with_bound_task() -> tuple[CodingAgentApp, ProjectSession, TaskSession]:
    app = make_logic_only_app()
    project = ProjectSession("p1", Path("."), "Demo")
    task = TaskSession(
        id="task-1",
        project_id=project.id,
        title="Chat",
        agent=FakeAgent(project.path),
        cancel_event=threading.Event(),
    )
    app.projects.append(project)
    app.tasks.append(task)
    app.current_id = task.id
    return app, project, task


def test_new_task_is_projectless_even_when_a_project_is_selected() -> None:
    app = make_logic_only_app()
    app.projects = [ProjectSession("p1", Path("."), "Demo")]

    app.new_task()

    assert app.tasks[-1].project_id is None
    assert app.tasks[-1].agent.workspace is None


def test_sidebar_groups_projectless_conversations_before_projects() -> None:
    app, task = make_app_with_projectless_task()
    project = ProjectSession("p1", Path("project"), "Project")
    project_task = TaskSession(
        id="task-2",
        project_id=project.id,
        title="Project task",
        agent=FakeAgent(project.path),
        cancel_event=threading.Event(),
    )
    app.projects.append(project)
    app.tasks.append(project_task)
    app.task_tree = RecordingTree()

    CodingAgentApp._refresh_task_tree(app)

    root_rows = [
        (item_id, options["text"])
        for item_id, (parent, options) in app.task_tree.items.items()
        if parent == ""
    ]
    assert root_rows == [("section:conversations", "对话"), ("section:projects", "项目")]
    assert app.task_tree.items[f"task:{task.id}"][0] == "section:conversations"
    assert app.task_tree.items[f"project:{project.id}"][0] == "section:projects"
    assert app.task_tree.items[f"task:{project_task.id}"][0] == f"project:{project.id}"


def test_binding_task_preserves_non_system_history(tmp_path: Path) -> None:
    app, task = make_app_with_projectless_task()
    task.agent.history = [
        {"role": "system", "content": "projectless"},
        {"role": "user", "content": "hello"},
    ]

    project = app._bind_task_to_path(task, tmp_path)

    assert task.project_id == project.id
    assert task.agent.history[1:] == [{"role": "user", "content": "hello"}]


def test_removing_project_keeps_its_tasks_projectless() -> None:
    app, project, task = make_app_with_bound_task()

    app._remove_project(project)

    assert project not in app.projects
    assert task in app.tasks
    assert task.project_id is None
    assert task.agent.workspace is None


def test_loads_missing_project_task_as_projectless_and_restores_current(tmp_path: Path) -> None:
    app = make_logic_only_app()
    app.store = SimpleNamespace(
        load=lambda: {
            "version": 2,
            "current_id": "task-1",
            "projects": [],
            "tasks": [
                {
                    "id": "task-1",
                    "project_id": "missing",
                    "title": "Chat",
                    "title_is_custom": True,
                    "entries": [],
                    "history": [{"role": "system", "content": "old project"}],
                }
            ],
        }
    )

    app._load_sessions()

    task = app.tasks[0]
    assert task.project_id is None
    assert task.agent.workspace is None
    assert task.title_is_custom is True
    assert app.current_id == task.id


@pytest.mark.parametrize("raw_path", ("", "   \t"))
def test_load_skips_blank_project_paths_and_keeps_referenced_tasks_projectless(raw_path: str) -> None:
    app = make_logic_only_app()
    app.store = SimpleNamespace(
        load=lambda: {
            "version": 2,
            "current_id": "task-1",
            "projects": [{"id": "invalid", "title": "Invalid", "path": raw_path}],
            "tasks": [{"id": "task-1", "project_id": "invalid", "title": "Chat", "entries": [], "history": []}],
        }
    )

    app._load_sessions()

    assert app.projects == []
    assert app.tasks[0].project_id is None
    assert app.tasks[0].agent.workspace is None


def test_saves_version_three_with_conversation_changes(tmp_path: Path) -> None:
    app, project, task = make_app_with_bound_task()
    project.path = tmp_path
    task.change_tracker.retarget(tmp_path)
    saved: list[dict[str, object]] = []
    app.store = SimpleNamespace(save=saved.append)
    task.title_is_custom = True
    capture = task.change_tracker.capture_paths(["a.txt"])
    (project.path / "a.txt").write_text("changed\n", encoding="utf-8")
    task.change_tracker.finish(capture)
    task.entries.append(gui.ChatEntry("tool", "changed", ("a.txt",)))
    task.review_path = "a.txt"

    CodingAgentApp._save_sessions(app)

    payload = saved[0]
    assert payload["version"] == 3
    assert payload["tasks"][0]["file_changes"] == task.change_tracker.serialize()
    assert payload["tasks"][0]["entries"][0]["change_paths"] == ["a.txt"]
    assert payload["tasks"][0]["review_path"] == "a.txt"


def test_tool_end_adds_clickable_change_paths_to_entry(tmp_path: Path) -> None:
    app, _project, task = make_app_with_bound_task()
    task.change_tracker = ConversationChangeTracker(tmp_path)
    app._set_status = lambda *_args: None
    app._render_transcript = lambda *_args: None
    app._save_sessions = lambda: None
    data = {
        "name": "write_file",
        "ok": True,
        "output": "ok",
        "error": None,
        "changes": {"paths": ["a.txt"], "warning": None, "files": []},
    }

    app._handle_agent_event(task.id, "tool_end", data)

    assert task.entries[-1].change_paths == ("a.txt",)


def test_open_change_only_reads_current_task(tmp_path: Path) -> None:
    app, first, second = make_app_with_two_tasks()
    first.change_tracker = ConversationChangeTracker(tmp_path)
    first_capture = first.change_tracker.capture_paths(["first.py"])
    (tmp_path / "first.py").write_text("first\n", encoding="utf-8")
    first.change_tracker.finish(first_capture)
    second.change_tracker = ConversationChangeTracker(tmp_path)
    second_capture = second.change_tracker.capture_paths(["second.py"])
    (tmp_path / "second.py").write_text("second\n", encoding="utf-8")
    second.change_tracker.finish(second_capture)
    shown: list[str] = []
    app.review_pane = SimpleNamespace(show_change=lambda change: shown.append(change.path))
    app._show_review_container = lambda: None

    app.current_id = first.id
    app._open_change("first.py")

    assert shown == ["first.py"]
    assert first.review_path == "first.py"
    assert second.review_path is None


def test_first_message_does_not_replace_a_custom_title(monkeypatch) -> None:
    app, task = make_app_with_projectless_task()
    task.title = "新对话 自定义名称"
    task.title_is_custom = True
    app.input_box = SimpleNamespace(get=lambda _start, _end: "hello", delete=lambda _start, _end: None)
    monkeypatch.setattr(gui.threading, "Thread", lambda **_kwargs: SimpleNamespace(start=lambda: None))

    CodingAgentApp.send_message(app)

    assert task.title == "新对话 自定义名称"


def test_enter_sends_without_inserting_newline() -> None:
    app = make_logic_only_app()
    called = []
    app.send_message = lambda: called.append(True)

    assert app._send_enter(None) == "break"
    assert called == [True]


def test_shift_enter_inserts_newline() -> None:
    app = make_logic_only_app()
    app.input_box = RecordingText()

    assert app._insert_newline(None) == "break"
    assert app.input_box.insertions == [("insert", "\n")]


def test_choose_workspace_binds_current_task_to_selected_directory(monkeypatch, tmp_path: Path) -> None:
    app, task = make_app_with_projectless_task()
    app.root = object()
    app.config = SimpleNamespace(workspace=tmp_path)
    selected = tmp_path / "workspace"
    selected.mkdir()
    asked: list[dict[str, object]] = []
    monkeypatch.setattr(
        gui.filedialog,
        "askdirectory",
        lambda **options: asked.append(options) or str(selected),
    )

    app.choose_workspace_for_current()

    assert task.project_id is not None
    assert task.agent.workspace == selected.resolve()
    assert asked == [{"parent": app.root, "initialdir": str(tmp_path), "title": "选择工作目录"}]


def test_choose_workspace_does_not_open_picker_for_running_task(monkeypatch) -> None:
    app, task = make_app_with_projectless_task()
    app.root = object()
    task.running = True
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **_options: pytest.fail("picker should not open"))
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *args, **_kwargs: warnings.append(args))

    app.choose_workspace_for_current()

    assert task.project_id is None
    assert warnings == [("任务运行中", "请先停止任务，再更改工作目录。")]


def test_choose_workspace_keeps_current_task_when_picker_is_cancelled(monkeypatch, tmp_path: Path) -> None:
    app, task = make_app_with_projectless_task()
    app.root = object()
    app.config = SimpleNamespace(workspace=tmp_path)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **_options: "")

    app.choose_workspace_for_current()

    assert task.project_id is None
    assert task.agent.workspace is None


def test_choose_workspace_reports_binding_error_without_changing_task(monkeypatch, tmp_path: Path) -> None:
    app, task = make_app_with_projectless_task()
    app.root = object()
    app.config = SimpleNamespace(workspace=tmp_path)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **_options: str(tmp_path))
    monkeypatch.setattr(app, "_bind_task_to_path", lambda *_args: (_ for _ in ()).throw(ValueError("不存在")))
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, message, **_kwargs: errors.append((title, message)))

    app.choose_workspace_for_current()

    assert task.project_id is None
    assert task.agent.workspace is None
    assert errors == [("工作目录无效", "无法使用所选工作目录：不存在")]


def test_composer_menu_uses_projectless_workspace_label(monkeypatch) -> None:
    app, _task = make_app_with_projectless_task()
    app.root = object()
    app.composer_menu_button = SimpleNamespace(
        winfo_rootx=lambda: 20,
        winfo_rooty=lambda: 30,
        winfo_height=lambda: 10,
    )
    created: list[object] = []

    class RecordingMenu:
        def __init__(self, *_args, **_kwargs) -> None:
            self.commands: list[dict[str, object]] = []
            self.popup: tuple[int, int] | None = None
            self.released = False
            self.destroyed = False
            created.append(self)

        def add_command(self, **options: object) -> None:
            self.commands.append(options)

        def tk_popup(self, x_root: int, y_root: int) -> None:
            self.popup = (x_root, y_root)

        def grab_release(self) -> None:
            self.released = True

        def destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr(gui.tk, "Menu", RecordingMenu)

    app._show_composer_menu()
    app._show_composer_menu()

    assert len(created) == 2
    assert created[0].commands[0]["label"] == "选择工作目录…"
    assert created[0].popup == (20, 40)
    assert created[0].released is True
    assert created[0].destroyed is True
    assert created[1].destroyed is False
    assert app._active_menu is created[1]


def test_projectless_rendering_labels_workspace_and_empty_state() -> None:
    app, task = make_app_with_projectless_task()
    app.title_label = RecordingWidget()
    app.project_label = RecordingWidget()
    app.project_menu_button = RecordingWidget()
    app.workspace_label = RecordingWidget()
    app.send_button = RecordingWidget()
    app.stop_button = RecordingWidget()
    app.input_box = RecordingWidget()
    app.transcript = RecordingTranscript()
    app._set_status = lambda *_args: None

    CodingAgentApp._render_current(app)

    assert app.project_label.configurations[-1] == {"text": "未选择工作目录 / 对话"}
    assert app.workspace_label.configurations[-1] == {"text": "尚未选择工作目录"}
    assert app.send_button.configurations[-1] == {"state": "normal"}
    assert ("end", "＋ 可启用本地文件操作\n", "empty_hint") in app.transcript.insertions


@pytest.mark.parametrize(("raw", "expected"), [("  新名字  ", "新名字"), ("a\nb", "a b"), ("   ", "")])
def test_normalize_display_name(raw: str, expected: str) -> None:
    assert normalize_display_name(raw) == expected


def test_rename_uses_menu_target_not_current_task() -> None:
    app, first, second = make_app_with_two_tasks()
    app.current_id = first.id

    assert app._rename_tree_item(f"task:{second.id}", "Second renamed") is True

    assert first.title != "Second renamed"
    assert second.title == "Second renamed"
    assert second.title_is_custom is True


def test_rename_project_uses_target_without_mutating_its_path() -> None:
    app, first, second, _first_task, _second_task = make_app_with_two_projects()
    original_path = second.path

    assert app._rename_tree_item(f"project:{second.id}", "  Renamed project  ") is True

    assert first.title == "First"
    assert second.title == "Renamed project"
    assert second.path == original_path


def test_project_removal_uses_target_not_selected_project(monkeypatch) -> None:
    app, first, second, first_task, second_task = make_app_with_two_projects()
    app.root = object()
    app.task_tree = SimpleNamespace(selection=lambda: (f"project:{first.id}",))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *_args, **_kwargs: True)

    app.delete_task(f"project:{second.id}")

    assert first in app.projects
    assert second not in app.projects
    assert first_task.project_id == first.id
    assert second_task.project_id is None


def test_item_menu_remains_active_until_the_composer_menu_replaces_it(monkeypatch) -> None:
    app, task = make_app_with_projectless_task()
    app.root = object()
    app.composer_menu_button = SimpleNamespace(
        winfo_rootx=lambda: 40,
        winfo_rooty=lambda: 50,
        winfo_height=lambda: 10,
    )
    created: list[object] = []

    class RecordingMenu:
        def __init__(self, *_args, **_kwargs) -> None:
            self.destroyed = False
            self.released = False
            created.append(self)

        def add_command(self, **_kwargs) -> None:
            pass

        def add_separator(self) -> None:
            pass

        def tk_popup(self, *_args) -> None:
            pass

        def grab_release(self) -> None:
            self.released = True

        def destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr(gui.tk, "Menu", RecordingMenu)

    app._show_item_menu(f"task:{task.id}", 20, 30)
    app._show_composer_menu()

    assert len(created) == 2
    assert created[0].released is True
    assert created[0].destroyed is True
    assert created[1].destroyed is False
    assert app._active_menu is created[1]


def test_header_project_menu_is_rename_only_and_captures_its_project_target(monkeypatch) -> None:
    app, first, second, _first_task, _second_task = make_app_with_two_projects()
    app.root = object()
    app.project_menu_button = SimpleNamespace(
        winfo_rootx=lambda: 20,
        winfo_rooty=lambda: 30,
        winfo_height=lambda: 10,
    )
    created: list[object] = []
    renamed: list[str] = []

    class RecordingMenu:
        def __init__(self, *_args, **_kwargs) -> None:
            self.commands: list[dict[str, object]] = []
            created.append(self)

        def add_command(self, **options: object) -> None:
            self.commands.append(options)

        def tk_popup(self, *_args) -> None:
            pass

        def grab_release(self) -> None:
            pass

        def destroy(self) -> None:
            pass

    monkeypatch.setattr(gui.tk, "Menu", RecordingMenu)
    app._prompt_rename_tree_item = renamed.append

    app._show_current_project_menu()
    app.current_id = second.id
    created[0].commands[0]["command"]()

    assert [command["label"] for command in created[0].commands] == ["重命名"]
    assert renamed == [f"project:{first.id}"]
