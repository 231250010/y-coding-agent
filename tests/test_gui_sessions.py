from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent import gui
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
    app._make_agent = lambda _task_id, _cancel_event, workspace: FakeAgent(workspace)
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


def test_projectless_task_is_visible_in_the_sidebar() -> None:
    app, task = make_app_with_projectless_task()
    app.task_tree = RecordingTree()

    CodingAgentApp._refresh_task_tree(app)

    assert app.task_tree.items[f"task:{task.id}"][0] == ""


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


def test_saves_version_two_projects_and_tasks_separately(tmp_path: Path) -> None:
    app, project, task = make_app_with_bound_task()
    saved: list[dict[str, object]] = []
    app.store = SimpleNamespace(save=saved.append)
    task.title_is_custom = True

    CodingAgentApp._save_sessions(app)

    assert saved == [
        {
            "version": 2,
            "current_id": task.id,
            "projects": [{"id": project.id, "title": "Demo", "path": str(project.path)}],
            "tasks": [
                {
                    "id": task.id,
                    "project_id": project.id,
                    "title": "Chat",
                    "title_is_custom": True,
                    "entries": [],
                    "history": task.agent.history,
                }
            ],
        }
    ]


def test_first_message_does_not_replace_a_custom_title(monkeypatch) -> None:
    app, task = make_app_with_projectless_task()
    task.title = "新对话 自定义名称"
    task.title_is_custom = True
    app.input_box = SimpleNamespace(get=lambda _start, _end: "hello", delete=lambda _start, _end: None)
    monkeypatch.setattr(gui.threading, "Thread", lambda **_kwargs: SimpleNamespace(start=lambda: None))

    CodingAgentApp.send_message(app)

    assert task.title == "新对话 自定义名称"


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


def test_item_menu_destroys_transient_menu_after_it_unposts(monkeypatch) -> None:
    app, task = make_app_with_projectless_task()
    app.root = object()
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

    assert len(created) == 1
    assert created[0].released is True
    assert created[0].destroyed is True
