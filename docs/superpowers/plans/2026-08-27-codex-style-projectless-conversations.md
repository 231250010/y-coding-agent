# Codex-Style Projectless Conversations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make new conversations projectless by default, allow normal chat without file tools, bind a working directory from the composer `＋` menu, add Codex-style rename menus, and send with Enter.

**Architecture:** `ToolRegistry` gains an explicit no-workspace mode so the existing `CodingAgent` loop can handle both ordinary chat and workspace-backed coding without duplicated logic. Session persistence moves to a normalized version-2 format with projects and tasks stored independently, while the Tkinter view renders nullable project relationships and owns directory binding, menus, and keyboard behavior.

**Tech Stack:** Python 3.11+, Tkinter/ttk, dataclasses, JSON persistence, pytest

**Spec:** `docs/superpowers/specs/2026-08-27-codex-style-projectless-conversations-design.md`

## Global Constraints

- New conversations and `Ctrl + N` always create `project_id=None` tasks.
- Projectless tasks receive no file, search, write, or command tool schemas.
- Existing version-1 sessions must load without losing projects, task entries, or model history.
- Removing a project never deletes files or conversations; its conversations become projectless.
- `Enter` sends, `Shift + Enter` inserts a newline, and `Ctrl + Enter` remains a send shortcut.
- Project renaming only changes the application display name, never the filesystem directory.
- Preserve the existing blueberry, milk-white, peach, Microsoft YaHei UI, and Cascadia Mono visual system.
- Preserve path guarding, command approval, task cancellation, and session-save safety behavior.

---

### Task 1: Explicit No-Workspace Tool Mode

**Files:**
- Modify: `src/coding_agent/tools.py`
- Modify: `src/coding_agent/prompts.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `ToolRegistry(workspace: Path | None, ...)`
- Produces: `ToolRegistry.workspace: Path | None`
- Produces: `PROJECTLESS_SYSTEM_PROMPT: str`
- Preserves: `ToolRegistry(Path(...))` and all existing tool schemas and execution behavior

- [ ] **Step 1: Write the failing disabled-registry test**

```python
def test_projectless_registry_exposes_no_tools() -> None:
    registry = ToolRegistry(None)

    assert registry.workspace is None
    assert registry.schemas() == []
    result = registry.execute("read_file", {"path": "README.md"})
    assert result.ok is False
    assert result.error == "当前对话尚未选择工作目录"
```

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools.py::test_projectless_registry_exposes_no_tools -q`

Expected: FAIL because `ToolRegistry(None)` attempts `None.resolve()`.

- [ ] **Step 3: Implement the no-workspace registry mode**

Change the constructor to accept `Path | None`. When `workspace is None`, set `self.workspace`, `self.guard`, and `self.policy` to `None`, create an empty `_tools` mapping, and return before building tools. In `execute`, return `ToolResult(False, error="当前对话尚未选择工作目录")` when the registry is disabled. Keep the existing unknown-tool error for workspace-backed registries.

Add to `prompts.py`:

```python
PROJECTLESS_SYSTEM_PROMPT = SYSTEM_PROMPT + """

当前对话尚未选择工作目录。你可以正常回答一般问题，但不能声称已经读取、修改或运行本地文件。
如果任务需要访问本地项目，请明确提示用户点击输入框左下角的“＋”并选择工作目录。
"""
```

- [ ] **Step 4: Run tool and agent regression tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tools.py tests/test_agent.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the focused change**

```powershell
git add src/coding_agent/tools.py src/coding_agent/prompts.py tests/test_tools.py
git commit -m "feat: support projectless agent conversations"
```

### Task 2: Version-2 Session Normalization

**Files:**
- Modify: `src/coding_agent/session_store.py`
- Test: `tests/test_session_store.py`

**Interfaces:**
- Produces: `empty_session_state() -> dict[str, Any]`
- Produces: `normalize_session_state(value: Any) -> dict[str, Any]`
- `SessionStore.load()` always returns version 2 with top-level `projects`, `tasks`, and nullable `current_id`

- [ ] **Step 1: Write failing migration and round-trip tests**

```python
def test_load_migrates_version_one_nested_tasks(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save({
        "version": 1,
        "projects": [{
            "id": "project-1", "title": "Demo", "path": str(tmp_path),
            "tasks": [{"id": "task-1", "title": "Fix", "entries": [], "history": []}],
        }],
    })

    state = store.load()

    assert state["version"] == 2
    assert state["projects"] == [{"id": "project-1", "title": "Demo", "path": str(tmp_path)}]
    assert state["tasks"][0]["project_id"] == "project-1"


def test_version_two_preserves_projectless_tasks(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    payload = {
        "version": 2, "current_id": "task-1", "projects": [],
        "tasks": [{
            "id": "task-1", "project_id": None, "title": "Chat",
            "title_is_custom": True, "entries": [], "history": [],
        }],
    }
    store.save(payload)

    assert store.load() == payload
```

- [ ] **Step 2: Run the focused tests and verify migration fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_store.py -q`

Expected: FAIL because the current loader returns version 1 and nested tasks.

- [ ] **Step 3: Implement normalization**

`normalize_session_state` must:

- Return `{"version": 2, "current_id": None, "projects": [], "tasks": []}` for invalid payloads.
- For version 1, copy project metadata without nested `tasks`, flatten valid task dictionaries, and inject `project_id` plus `title_is_custom=False`.
- For version 2, copy valid project/task dictionaries, normalize missing `project_id` to `None`, normalize `title_is_custom` to `bool`, and preserve a string `current_id` only.
- Never mutate the input payload.

Call the normalizer from `SessionStore.load`; leave `save` atomic and format-agnostic.

- [ ] **Step 4: Run session-store tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the storage migration**

```powershell
git add src/coding_agent/session_store.py tests/test_session_store.py
git commit -m "feat: migrate sessions to projectless format"
```

### Task 3: Projectless Task Lifecycle and Safe Rebinding

**Files:**
- Modify: `src/coding_agent/gui.py`
- Create: `tests/test_gui_sessions.py`

**Interfaces:**
- Changes: `TaskSession.project_id: str | None`
- Adds: `TaskSession.title_is_custom: bool = False`
- Changes: `CodingAgentApp._make_agent(task_id, cancel_event, workspace: Path | None) -> CodingAgent`
- Adds: `CodingAgentApp._retarget_agent(session: TaskSession, project: ProjectSession | None) -> None`
- Adds: `CodingAgentApp._ensure_project(path: Path) -> ProjectSession`
- Adds: `CodingAgentApp._bind_task_to_path(session: TaskSession, path: Path) -> ProjectSession`

- [ ] **Step 1: Write failing lifecycle tests using an app constructed with `__new__`**

Use lightweight fake agents with `history` and fake stores; do not create a Tk root. Cover these behaviors:

```python
def test_new_task_is_projectless_even_when_a_project_is_selected() -> None:
    app = make_logic_only_app()
    app.projects = [ProjectSession("p1", Path("."), "Demo")]
    app.new_task()
    assert app.tasks[-1].project_id is None


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
```

- [ ] **Step 2: Run the new lifecycle tests and verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_sessions.py -q`

Expected: FAIL because tasks require a project and rebinding helpers do not exist.

- [ ] **Step 3: Implement nullable project lifecycle**

- Change app startup to call `new_task()` when no stored tasks exist; do not call `_add_project(config.workspace)`.
- Make `new_task` ignore current selection and create an agent with `workspace=None`.
- Select `PROJECTLESS_SYSTEM_PROMPT` in `_make_agent` when `workspace is None`.
- `_retarget_agent` creates the correct agent, replaces history index 0 with the new agent system message, then appends the old history from index 1.
- `_ensure_project` resolves paths, returns an existing path match or creates a project without creating a task.
- `_bind_task_to_path` rejects running tasks, ensures the project, updates `project_id`, retargets the agent, refreshes state, and returns the project.
- `_remove_project` rejects projects with running tasks, removes only the project, clears each matching task's `project_id`, and retargets those tasks to no-workspace mode.
- Preserve automatic first-message titles only when `title_is_custom` is false.

- [ ] **Step 4: Update load/save for normalized version 2**

Load projects first, then top-level tasks. If a task references a missing or inaccessible project, load it with `project_id=None` and a no-workspace agent. Restore `current_id` when valid. Save project metadata and tasks in separate top-level lists with `version: 2`.

- [ ] **Step 5: Run lifecycle and existing GUI-boundary tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_sessions.py tests/test_cli_and_boundaries.py tests/test_gui_design.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the lifecycle change**

```powershell
git add src/coding_agent/gui.py tests/test_gui_sessions.py
git commit -m "feat: add projectless task lifecycle"
```

### Task 4: Codex-Style Rename Menus

**Files:**
- Modify: `src/coding_agent/gui.py`
- Modify: `tests/test_gui_sessions.py`

**Interfaces:**
- Adds: `normalize_display_name(value: str) -> str`
- Adds: `RenameDialog(parent, title: str, label: str, initial: str)` with `result: str | None`
- Adds: `CodingAgentApp._rename_tree_item(item_id: str, name: str) -> bool`
- Adds: `CodingAgentApp._show_item_menu(item_id: str, x_root: int, y_root: int) -> None`
- Adds: `CodingAgentApp._show_tree_actions(event) -> None`
- Adds: `CodingAgentApp._hide_tree_actions(event=None) -> None`

- [ ] **Step 1: Write failing name and target tests**

```python
@pytest.mark.parametrize(("raw", "expected"), [
    ("  新名字  ", "新名字"),
    ("a\nb", "a b"),
    ("   ", ""),
])
def test_normalize_display_name(raw: str, expected: str) -> None:
    assert normalize_display_name(raw) == expected


def test_rename_uses_menu_target_not_current_task() -> None:
    app, first, second = make_app_with_two_tasks()
    app.current_id = first.id
    assert app._rename_tree_item(f"task:{second.id}", "Second renamed") is True
    assert first.title != "Second renamed"
    assert second.title == "Second renamed"
    assert second.title_is_custom is True
```

- [ ] **Step 2: Run the focused tests and verify missing interfaces fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_sessions.py -q`

Expected: FAIL because rename helpers do not exist.

- [ ] **Step 3: Implement validation and rename dialog**

`normalize_display_name` replaces newlines with spaces, collapses repeated whitespace, trims the result, and caps names at 80 characters. `RenameDialog` uses current visual tokens, selects the initial name, saves on Enter, cancels on Esc, and displays “名称不能为空” without closing for an empty result.

- [ ] **Step 4: Implement sidebar hover and context menus**

- Bind Treeview `<Motion>`, `<Leave>`, and `<Button-3>`.
- Track `_hover_item`; place a compact `···` button at the right edge of the hovered project/task row using `task_tree.bbox(item_id)`.
- Hide the button for grouping rows, invalid rows, and when the pointer leaves both the row and button.
- Build menus using the explicit `item_id` passed by the click/right-click event.
- Route existing delete/remove confirmations through target-aware helpers so a context menu never acts on an unrelated current task.

- [ ] **Step 5: Add the right-header project menu**

Show a `···` button beside the bound project label; hide it for projectless tasks. Its menu calls the same project rename helper as the sidebar.

- [ ] **Step 6: Run rename and GUI design tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_sessions.py tests/test_gui_design.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the menu feature**

```powershell
git add src/coding_agent/gui.py tests/test_gui_sessions.py
git commit -m "feat: add codex-style rename menus"
```

### Task 5: Composer Directory Menu and Enter-to-Send

**Files:**
- Modify: `src/coding_agent/gui.py`
- Modify: `tests/test_gui_sessions.py`

**Interfaces:**
- Adds: `CodingAgentApp.choose_workspace_for_current() -> None`
- Adds: `CodingAgentApp._show_composer_menu() -> None`
- Adds: `CodingAgentApp._send_enter(event) -> str`
- Adds: `CodingAgentApp._insert_newline(event) -> str`

- [ ] **Step 1: Write failing keyboard behavior tests**

Use a fake input box that records `insert` and a fake `send_message` callback:

```python
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
```

- [ ] **Step 2: Run the keyboard tests and verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_sessions.py -q`

Expected: FAIL because the event handlers do not exist.

- [ ] **Step 3: Implement keyboard bindings**

Bind `<Return>` to `_send_enter`, `<Shift-Return>` to `_insert_newline`, and retain `<Control-Return>` mapped to `_send_shortcut`. Every handler returns `"break"` so Tk class bindings cannot insert an extra newline or send twice.

- [ ] **Step 4: Add the composer `＋` menu**

Place a blueberry-on-milk `＋` button at the left of the workspace label. The popup contains one item: “选择工作目录…” when projectless, or “更换工作目录…” when bound. `choose_workspace_for_current` starts from the bound directory or configured workspace, returns unchanged on picker cancellation, rejects running tasks, and calls `_bind_task_to_path` for valid selections.

- [ ] **Step 5: Update projectless copy and workspace rendering**

- Header: `未选择工作目录 / 对话` when projectless.
- Composer path: `尚未选择工作目录` when projectless.
- Empty state retains current “小码” suggestions and adds a compact hint that `＋` enables local files only when no project is bound.
- The send button remains enabled for non-running projectless tasks.

- [ ] **Step 6: Run focused GUI tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_sessions.py tests/test_gui_design.py -q`

Expected: PASS.

- [ ] **Step 7: Commit composer interactions**

```powershell
git add src/coding_agent/gui.py tests/test_gui_sessions.py tests/test_gui_design.py
git commit -m "feat: add workspace composer menu and enter send"
```

### Task 6: Integration, Review, and Application Restart

**Files:**
- Verify: `src/coding_agent/gui.py`
- Verify: `src/coding_agent/tools.py`
- Verify: `src/coding_agent/session_store.py`
- Verify: all tests

**Interfaces:**
- Consumes all prior task interfaces.
- Produces a running updated desktop application after validation.

- [ ] **Step 1: Run the full automated suite and static checks**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest -q
& '.venv\Scripts\python.exe' -m compileall -q src
git diff --check
```

Expected: all tests pass and both later commands exit 0.

- [ ] **Step 2: Request an independent code review**

Review the implementation against the approved spec, with emphasis on data migration, no-workspace tool isolation, wrong-target menu actions, history preservation, and keyboard event ordering. Fix all Critical and Important findings with a failing regression test first.

- [ ] **Step 3: Perform visual verification**

Launch a preview using isolated temporary session storage. Inspect 1240×800 and 960×640 states and verify:

- projectless header and composer `＋` are visible;
- send controls remain visible;
- hover `···` aligns with both task and project rows;
- sidebar and header menus open beside their trigger;
- rename dialog focus, Enter save, and Esc cancel work;
- selecting a directory moves the current task under the correct project without losing transcript content.

- [ ] **Step 4: Close all old preview and application windows**

Use exact returned window titles/handles. Do not terminate unrelated Python processes.

- [ ] **Step 5: Start the updated application**

Launch `.venv\Scripts\pythonw.exe -m coding_agent.gui --workspace C:\Users\30959\Desktop\codingagent` and confirm exactly one updated “小码 · 本地代码工作台” window appears.

- [ ] **Step 6: Report evidence**

Report the exact test count, visual sizes checked, migration behavior verified, review findings addressed, files changed, and that the updated window was restarted.
