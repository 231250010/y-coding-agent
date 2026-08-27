# Conversation File Diff Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track files changed by Agent tools per conversation and expose persistent, clickable, color-highlighted cumulative diffs in an on-demand right review pane.

**Architecture:** A GUI-independent `ConversationChangeTracker` captures bounded before/after snapshots and owns cumulative change segments. `ToolRegistry` attaches local-only change data to `ToolResult`, `CodingAgent` forwards it through events, and the Tkinter GUI persists the data and renders it through a focused `diff_view.py` component. Chat Completions messages and tool schemas remain unchanged.

**Tech Stack:** Python 3.11+, standard library (`dataclasses`, `difflib`, `hashlib`, `pathlib`, `tkinter`), pytest, existing `openai` base client and Rich dependency boundaries.

**Spec:** `docs/superpowers/specs/2026-08-28-conversation-file-diff-review-design.md`

## Global Constraints

- Only changes produced inside Agent tool execution boundaries count; do not use Git HEAD as the baseline.
- Track `write_file`, `replace_text`, and `run_command`; read-only tools remain untracked.
- Support Git and non-Git workspaces on Windows and common POSIX environments.
- Keep snapshot and diff metadata local; `ToolResult.to_message()` must never serialize it to the model.
- Preserve workspace path guarding and reject external symlink targets.
- Persist bounded historical diffs locally in `.coding-agent/sessions.json` and migrate session versions 1 and 2 to version 3.
- Keep API keys and environment-variable values out of repository files, logs, tool metadata, and tests.
- Add no Agent framework, hosted file tool, Code Interpreter, Files API, or new runtime dependency.
- Use English identifiers and comments; use Chinese GUI labels and user-facing messages.
- Automated tests must not call DeepSeek or any real model API.
- Apply the approved visual tokens globally: sidebar `#244A67`, workspace `#F7F5F0`, surface `#FFFFFF`, accent `#F2A97E`, added `#DDF4E5/#177245`, removed `#FCE1E1/#B33A3A`.

## File Map

- Create `src/coding_agent/changes.py`: snapshots, diff rows, cumulative segments, workspace scans, serialization, and drift detection.
- Modify `src/coding_agent/tools.py`: capture modifying tool boundaries and attach a local `ChangeSet` to `ToolResult`.
- Modify `src/coding_agent/agent.py`: forward serialized change summaries in `tool_end` without altering model messages.
- Modify `src/coding_agent/session_store.py`: version 3 normalization, migration, and validation.
- Create `src/coding_agent/diff_view.py`: file cards and the right-side Tkinter Diff renderer.
- Modify `src/coding_agent/gui.py`: per-task tracker ownership, inner paned layout, event consumption, rendering, persistence, and approved global palette.
- Create `tests/test_changes.py`: pure tracking and diff tests.
- Modify `tests/test_tools.py`, `tests/test_agent.py`, `tests/test_session_store.py`, `tests/test_gui_design.py`, and `tests/test_gui_sessions.py`: integration and regression coverage.

---

### Task 1: Pure Snapshot, Diff, and Cumulative Change Model

**Files:**
- Create: `src/coding_agent/changes.py`
- Create: `tests/test_changes.py`

**Interfaces:**
- Produces: `FileSnapshot`, `DiffRow`, `ChangeSegment`, `FileChange`, `ChangeSet`, `Capture`, and `ConversationChangeTracker`.
- Produces: `ConversationChangeTracker.capture_paths(paths: Sequence[str]) -> Capture`.
- Produces: `ConversationChangeTracker.finish(capture: Capture) -> ChangeSet`.
- Produces: `ConversationChangeTracker.serialize() -> list[dict[str, Any]]` and `load_serialized(items: Any) -> None`.
- Consumes: only a resolved `Path | None`; it must not import `tools.py`, `agent.py`, or Tkinter.

- [ ] **Step 1: Write failing tests for text snapshots and cumulative changes**

```python
# tests/test_changes.py
from pathlib import Path

from coding_agent.changes import ConversationChangeTracker


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
```

- [ ] **Step 2: Run the focused tests and verify the missing-module failure**

Run: `python -m pytest tests/test_changes.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'coding_agent.changes'`.

- [ ] **Step 3: Implement the data model and text diff calculation**

```python
# src/coding_agent/changes.py
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

MAX_TEXT_BYTES = 1_048_576
MAX_COMMAND_SNAPSHOT_BYTES = 32 * 1_048_576


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    exists: bool
    size: int = 0
    digest: str | None = None
    text: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DiffRow:
    kind: str
    old_line: int | None
    new_line: int | None
    text: str


@dataclass(slots=True)
class ChangeSegment:
    workspace: str
    baseline: FileSnapshot
    latest: FileSnapshot
    drifted: bool = False


@dataclass(slots=True)
class FileChange:
    path: str
    segments: list[ChangeSegment]
    status: str
    added: int
    deleted: int
    binary: bool = False
    truncated: bool = False
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    paths: tuple[str, ...] = ()
    warning: str | None = None

    def to_event(self, changes: dict[str, FileChange]) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "warning": self.warning,
            "files": [file_change_to_dict(changes[path]) for path in self.paths if path in changes],
        }


@dataclass(frozen=True, slots=True)
class Capture:
    snapshots: dict[str, FileSnapshot]
    warning: str | None = None
    workspace_scan: bool = False


class ConversationChangeTracker:
    def __init__(self, workspace: Path | None, *, max_text_bytes: int = MAX_TEXT_BYTES,
                 max_command_bytes: int = MAX_COMMAND_SNAPSHOT_BYTES) -> None:
        self.workspace = workspace.resolve() if workspace else None
        self.max_text_bytes = max_text_bytes
        self.max_command_bytes = max_command_bytes
        self.changes: dict[str, FileChange] = {}

    def capture_paths(self, paths: Sequence[str]) -> Capture:
        snapshots = {self._relative(path): self._snapshot(self._relative(path)) for path in paths}
        return Capture(snapshots)

    def finish(self, capture: Capture) -> ChangeSet:
        changed: list[str] = []
        for path, before in capture.snapshots.items():
            after = self._snapshot(path)
            if before.digest == after.digest and before.exists == after.exists:
                continue
            self._merge(path, before, after)
            changed.append(path)
        return ChangeSet(tuple(changed), capture.warning)
```

Implement the remaining core methods with these exact rules:

```python
def _relative(self, user_path: str) -> str:
    if self.workspace is None:
        raise ValueError("当前对话尚未选择工作目录")
    raw = Path(user_path)
    candidate = raw if raw.is_absolute() else self.workspace / raw
    resolved = candidate.resolve(strict=False)
    try:
        return resolved.relative_to(self.workspace).as_posix()
    except ValueError as exc:
        raise ValueError(f"路径超出工作区: {user_path}") from exc

def _snapshot(self, relative: str, *, content_budget: int | None = None) -> FileSnapshot:
    assert self.workspace is not None
    path = (self.workspace / relative).resolve(strict=False)
    try:
        path.relative_to(self.workspace)
    except ValueError:
        return FileSnapshot(False, reason="路径超出工作区")
    if not path.exists():
        return FileSnapshot(False)
    if not path.is_file():
        return FileSnapshot(True, reason="不是普通文件")
    data: bytes | None = None
    for attempt in range(2):
        before_stat = path.stat()
        candidate = path.read_bytes()
        after_stat = path.stat()
        if (before_stat.st_size, before_stat.st_mtime_ns) == (after_stat.st_size, after_stat.st_mtime_ns):
            data = candidate
            break
        if attempt == 1:
            return FileSnapshot(True, after_stat.st_size, reason="扫描期间文件持续变化")
    assert data is not None
    digest = hashlib.sha256(data).hexdigest()
    if b"\x00" in data[:8192]:
        return FileSnapshot(True, len(data), digest, reason="二进制文件")
    limit = self.max_text_bytes if content_budget is None else min(self.max_text_bytes, content_budget)
    if len(data) > limit:
        reason = "总快照容量已用尽" if content_budget is not None and content_budget < len(data) else "文件超过文本预览上限"
        return FileSnapshot(True, len(data), digest, reason=reason)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return FileSnapshot(True, len(data), digest, reason="不是 UTF-8 文本")
    return FileSnapshot(True, len(data), digest, text=text)

def line_counts(old: str, new: str) -> tuple[int, int]:
    added = deleted = 0
    matcher = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return added, deleted

def build_diff_rows(old: str, new: str, context: int = 3) -> list[DiffRow]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[DiffRow] = []
    for group in matcher.get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        rows.append(DiffRow("hunk", None, None, f"@@ -{first[1] + 1},{last[2] - first[1]} +{first[3] + 1},{last[4] - first[3]} @@"))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows.extend(DiffRow("context", index + 1, j1 + index - i1 + 1, old_lines[index]) for index in range(i1, i2))
            elif tag in {"replace", "delete"}:
                rows.extend(DiffRow("removed", index + 1, None, old_lines[index]) for index in range(i1, i2))
            if tag in {"replace", "insert"}:
                rows.extend(DiffRow("added", None, index + 1, new_lines[index]) for index in range(j1, j2))
    return rows

def _merge(self, path: str, before: FileSnapshot, after: FileSnapshot) -> None:
    existing = self.changes.get(path)
    workspace = str(self.workspace) if self.workspace else ""
    if existing and (
        existing.segments[-1].workspace != workspace
        or existing.segments[-1].latest.digest != before.digest
        or existing.segments[-1].latest.exists != before.exists
    ):
        existing.segments[-1].drifted = True
        existing.segments.append(ChangeSegment(workspace, before, after))
    elif existing:
        existing.segments[-1].latest = after
    else:
        existing = FileChange(path, [ChangeSegment(workspace, before, after)], "modified", 0, 0)
        self.changes[path] = existing
    active = existing.segments[-1]
    existing.status = (
        "added" if not active.baseline.exists and active.latest.exists
        else "deleted" if active.baseline.exists and not active.latest.exists
        else "modified"
    )
    counts = [line_counts(segment.baseline.text or "", segment.latest.text or "") for segment in existing.segments]
    existing.added = sum(added for added, _deleted in counts)
    existing.deleted = sum(deleted for _added, deleted in counts)
    existing.binary = any(segment.baseline.reason == "二进制文件" or segment.latest.reason == "二进制文件" for segment in existing.segments)
    existing.truncated = any(segment.baseline.text is None or segment.latest.text is None for segment in existing.segments) and not existing.binary
    if len(existing.segments) == 1 and active.baseline.exists == active.latest.exists and active.baseline.digest == active.latest.digest:
        self.changes.pop(path, None)
```

Use explicit snapshot and segment codecs; invalid items are skipped individually instead of aborting the session:

```python
def _snapshot_to_dict(snapshot: FileSnapshot) -> dict[str, Any]:
    return {
        "exists": snapshot.exists, "size": snapshot.size, "digest": snapshot.digest,
        "text": snapshot.text, "reason": snapshot.reason,
    }

def _snapshot_from_dict(value: Any) -> FileSnapshot | None:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        return None
    size = value.get("size", 0)
    if not isinstance(size, int) or size < 0:
        return None
    digest, text, reason = value.get("digest"), value.get("text"), value.get("reason")
    if any(item is not None and not isinstance(item, str) for item in (digest, text, reason)):
        return None
    return FileSnapshot(value["exists"], size, digest, text, reason)

def serialize(self) -> list[dict[str, Any]]:
    return [{
        "path": change.path,
        "segments": [{
            "workspace": segment.workspace,
            "baseline": _snapshot_to_dict(segment.baseline),
            "latest": _snapshot_to_dict(segment.latest),
            "drifted": segment.drifted,
        } for segment in change.segments],
        "status": change.status, "added": change.added, "deleted": change.deleted,
        "binary": change.binary, "truncated": change.truncated, "warning": change.warning,
    } for change in self.changes.values()]

def load_serialized(self, items: Any) -> None:
    self.changes.clear()
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            continue
        segments: list[ChangeSegment] = []
        raw_segments = item.get("segments")
        if not isinstance(raw_segments, list):
            continue
        for raw in raw_segments:
            if not isinstance(raw, dict) or not isinstance(raw.get("workspace"), str):
                continue
            baseline = _snapshot_from_dict(raw.get("baseline"))
            latest = _snapshot_from_dict(raw.get("latest"))
            if baseline and latest:
                segments.append(ChangeSegment(raw["workspace"], baseline, latest, bool(raw.get("drifted", False))))
        if not segments:
            continue
        path = item["path"]
        counts = [line_counts(segment.baseline.text or "", segment.latest.text or "") for segment in segments]
        self.changes[path] = FileChange(
            path=path, segments=segments, status=str(item.get("status", "modified")),
            added=sum(value[0] for value in counts), deleted=sum(value[1] for value in counts),
            binary=bool(item.get("binary", False)), truncated=bool(item.get("truncated", False)),
            warning=item.get("warning") if isinstance(item.get("warning"), str) else None,
        )
```

- [ ] **Step 4: Add failing edge-case and serialization tests**

```python
def test_binary_and_large_files_keep_metadata_without_text(tmp_path: Path) -> None:
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"a\x00b")
    tracker = ConversationChangeTracker(tmp_path, max_text_bytes=4)
    capture = tracker.capture_paths(["blob.bin", "large.txt"])
    binary.write_bytes(b"a\x00c")
    (tmp_path / "large.txt").write_text("12345", encoding="utf-8")
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


def test_invalid_serialized_item_is_skipped(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    tracker.load_serialized([{"path": "../escape", "segments": "bad"}, 42])
    assert tracker.changes == {}
```

- [ ] **Step 5: Run the complete core test file**

Run: `python -m pytest tests/test_changes.py -v`

Expected: PASS, including added, modified, deleted, reverted, UTF-8, binary, large-file, traversal, symlink, diff-row, and serialization cases.

- [ ] **Step 6: Commit the pure tracking core**

```powershell
git add src/coding_agent/changes.py tests/test_changes.py
git commit -m "feat: add conversation change tracker"
```

---

### Task 2: File Tool Boundaries and Local-Only Agent Events

**Files:**
- Modify: `src/coding_agent/tools.py:24-33,75-120,307-335`
- Modify: `src/coding_agent/agent.py:66-81`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: `ConversationChangeTracker.capture_paths()` and `.finish()` from Task 1.
- Produces: `ToolResult.changes: ChangeSet` with an empty default.
- Produces: `ToolRegistry(..., change_tracker: ConversationChangeTracker | None = None)`.
- Produces: `tool_end` event key `changes`, using `ChangeSet.to_event(tracker.changes)`.
- Guarantees: `ToolResult.to_message()` excludes `changes`.

- [ ] **Step 1: Write failing tests for file-tool capture and model-message isolation**

```python
# tests/test_tools.py
from coding_agent.changes import ConversationChangeTracker


def test_write_and_replace_report_cumulative_local_changes(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)

    written = tools.execute("write_file", {"path": "a.txt", "content": "old\n"})
    replaced = tools.execute(
        "replace_text", {"path": "a.txt", "old_text": "old", "new_text": "new\nextra"}
    )

    assert written.changes.paths == ("a.txt",)
    assert replaced.changes.paths == ("a.txt",)
    assert (tracker.changes["a.txt"].added, tracker.changes["a.txt"].deleted) == (2, 0)
    assert "changes" not in written.to_message()


def test_failed_replace_that_does_not_write_has_no_changes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("same same", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, change_tracker=tracker)
    result = tools.execute("replace_text", {"path": "a.txt", "old_text": "same", "new_text": "x"})
    assert result.ok is False
    assert result.changes.paths == ()
```

```python
# tests/test_agent.py
def test_tool_end_exposes_local_changes_without_sending_them_to_model(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    tracker = ConversationChangeTracker(tmp_path)
    model = ScriptedModel([
        AssistantResponse(tool_calls=[call("c1", "write_file", {"path": "a.txt", "content": "hello\n"})]),
        AssistantResponse("done"),
    ])
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    agent = CodingAgent(model, tools, ContextManager(100_000), on_event=lambda name, data: events.append((name, data)))

    agent.run("write")

    event = next(data for name, data in events if name == "tool_end")
    assert event["changes"]["paths"] == ["a.txt"]
    assert "changes" not in model.requests[1][0][-1]["content"]
```

- [ ] **Step 2: Run focused tests and verify constructor/attribute failures**

Run: `python -m pytest tests/test_tools.py::test_write_and_replace_report_cumulative_local_changes tests/test_agent.py::test_tool_end_exposes_local_changes_without_sending_them_to_model -v`

Expected: FAIL because `ToolRegistry` has no `change_tracker` argument and `ToolResult` has no `changes` field.

- [ ] **Step 3: Extend `ToolResult` and wrap file tools**

```python
# src/coding_agent/tools.py
from dataclasses import dataclass, field, replace
from .changes import ChangeSet, ConversationChangeTracker


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: str = ""
    error: str | None = None
    changes: ChangeSet = field(default_factory=ChangeSet)

    def to_message(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.output:
            payload["output"] = self.output
        if self.error:
            payload["error"] = self.error
        return json.dumps(payload, ensure_ascii=False)
```

Store `self.change_tracker = change_tracker` in `ToolRegistry.__init__`. Replace `execute()` with a validation-first wrapper and move the current exception conversion into `_execute_handler`:

```python
def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
    if self.workspace is None:
        return ToolResult(False, error="当前对话尚未选择工作目录")
    tool = self._tools.get(name)
    if tool is None:
        return ToolResult(False, error=f"未知工具: {name}")
    validation_error = self._validate(arguments, tool.parameters)
    if validation_error:
        return ToolResult(False, error=validation_error)
    capture = None
    if self.change_tracker and name in {"write_file", "replace_text"}:
        capture = self.change_tracker.capture_paths([str(arguments["path"])])
    result = self._execute_handler(tool, arguments)
    if capture is not None:
        result = replace(result, changes=self.change_tracker.finish(capture))
    return result

def _execute_handler(self, tool: LocalTool, arguments: dict[str, Any]) -> ToolResult:
    try:
        return tool.handler(arguments)
    except (OSError, UnicodeError, ValueError) as exc:
        return ToolResult(False, error=str(exc))
    except Exception as exc:
        return ToolResult(False, error=f"工具执行异常: {type(exc).__name__}: {exc}")
```

Do not capture unknown tools, invalid JSON, invalid schemas, read-only tools, approval rejection, or safety-policy rejection.

- [ ] **Step 4: Forward local changes in `tool_end`**

```python
# src/coding_agent/agent.py
event_changes = result.changes.to_event(self.tools.change_tracker.changes) if self.tools.change_tracker else {
    "paths": [], "warning": None, "files": []
}
self.on_event(
    "tool_end",
    {
        "name": call.name,
        "ok": result.ok,
        "output": result.output,
        "error": result.error,
        "changes": event_changes,
    },
)
```

- [ ] **Step 5: Run file-tool and Agent suites**

Run: `python -m pytest tests/test_tools.py tests/test_agent.py -v`

Expected: PASS; existing plain answers, invalid JSON, unknown tools, repeated errors, cancellation, approval, and path safety remain green.

- [ ] **Step 6: Commit file-tool integration**

```powershell
git add src/coding_agent/tools.py src/coding_agent/agent.py tests/test_tools.py tests/test_agent.py
git commit -m "feat: report agent file tool changes"
```

---

### Task 3: Bounded Workspace Scanning for `run_command`

**Files:**
- Modify: `src/coding_agent/changes.py`
- Modify: `src/coding_agent/tools.py:103-118,337-410`
- Modify: `tests/test_changes.py`
- Modify: `tests/test_tools.py`

**Interfaces:**
- Produces: `ConversationChangeTracker.capture_workspace() -> Capture`.
- Consumes: Task 2's `ToolResult.changes` and execution wrapper.
- Guarantees: post-command capture runs after success, non-zero exit, timeout, cancellation, or recoverable execution error once the command process has started.

- [ ] **Step 1: Write failing scanner tests**

```python
# tests/test_changes.py
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
```

- [ ] **Step 2: Run scanner tests and verify the missing-method failure**

Run: `python -m pytest tests/test_changes.py -k workspace_capture -v`

Expected: FAIL with `AttributeError: 'ConversationChangeTracker' object has no attribute 'capture_workspace'`.

- [ ] **Step 3: Implement deterministic bounded workspace capture**

Add these constants and helpers to `changes.py`:

```python
IGNORED_DIRECTORY_NAMES = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".coding-agent", ".superpowers",
})

def capture_workspace(self) -> Capture:
    if self.workspace is None:
        return Capture({})
    snapshots: dict[str, FileSnapshot] = {}
    consumed = 0
    skipped_preview = 0
    for path in sorted(self.workspace.rglob("*"), key=lambda item: item.as_posix().lower()):
        relative_parts = path.relative_to(self.workspace).parts
        if any(part in IGNORED_DIRECTORY_NAMES for part in relative_parts):
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(self.workspace).as_posix()
        remaining = max(0, self.max_command_bytes - consumed)
        snapshot = self._snapshot(relative, content_budget=remaining)
        snapshots[relative] = snapshot
        if snapshot.text is not None:
            consumed += len(snapshot.text.encode("utf-8"))
        elif snapshot.exists and snapshot.reason == "总快照容量已用尽":
            skipped_preview += 1
    warning = f"预览不完整：{skipped_preview} 个文件未保存文本快照" if skipped_preview else None
    return Capture(snapshots, warning, workspace_scan=True)
```

Return `Capture(snapshots, warning, workspace_scan=True)`. Update `finish()` so command captures include files created after the initial scan:

```python
def finish(self, capture: Capture) -> ChangeSet:
    after_snapshots = (
        self._capture_workspace_snapshots(content_budget=self.max_command_bytes)
        if capture.workspace_scan else
        {path: self._snapshot(path) for path in capture.snapshots}
    )
    changed: list[str] = []
    for path in sorted(set(capture.snapshots) | set(after_snapshots)):
        before = capture.snapshots.get(path, FileSnapshot(False))
        after = after_snapshots.get(path, FileSnapshot(False))
        if before.exists == after.exists and before.digest == after.digest:
            continue
        self._merge(path, before, after)
        changed.append(path)
    return ChangeSet(tuple(changed), capture.warning)
```

Move the traversal loop into `_capture_workspace_snapshots(content_budget: int) -> dict[str, FileSnapshot]` so `capture_workspace()` and `finish()` share identical ignore rules and ordering.

- [ ] **Step 4: Write failing `run_command` integration tests**

```python
# tests/test_tools.py
def test_run_command_reports_created_modified_and_deleted_files(tmp_path: Path) -> None:
    (tmp_path / "edit.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("gone\n", encoding="utf-8")
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    command = (
        'python -c "from pathlib import Path; '
        "Path('edit.txt').write_text('after\\n'); "
        "Path('made.txt').write_text('made\\n'); Path('delete.txt').unlink()\""
    )

    result = tools.execute("run_command", {"command": command, "timeout_seconds": 10})

    assert result.ok is True
    assert result.changes.paths == ("delete.txt", "edit.txt", "made.txt")


def test_nonzero_command_still_reports_written_file(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    command = 'python -c "from pathlib import Path; import sys; Path(\'partial.txt\').write_text(\'x\'); sys.exit(7)"'
    result = tools.execute("run_command", {"command": command, "timeout_seconds": 10})
    assert result.ok is False
    assert result.changes.paths == ("partial.txt",)
```

- [ ] **Step 5: Wrap approved `run_command` execution with workspace capture**

Extend Task 2's wrapper with the command branch below. It begins after schema validation and always finishes after `_execute_handler` returns, including converted timeout, cancellation, non-zero, and exception results:

```python
if self.change_tracker and name in {"write_file", "replace_text"}:
    capture = self.change_tracker.capture_paths([str(arguments["path"])])
elif self.change_tracker and name == "run_command":
    capture = self.change_tracker.capture_workspace()
else:
    capture = None
result = self._execute_handler(tool, arguments)
if capture is not None:
    result = replace(result, changes=self.change_tracker.finish(capture))
return result
```

Approval or safety rejection may therefore pay the cost of a scan but produces an empty `ChangeSet`; it must not create file cards. This keeps policy logic inside `_run_command` and avoids duplicating command parsing.

- [ ] **Step 6: Run scanner and command tests**

Run: `python -m pytest tests/test_changes.py tests/test_tools.py -v`

Expected: PASS, including success, non-zero exit, timeout, cancellation, output truncation, ignored directories, binary files, and total snapshot caps.

- [ ] **Step 7: Commit command scanning**

```powershell
git add src/coding_agent/changes.py src/coding_agent/tools.py tests/test_changes.py tests/test_tools.py
git commit -m "feat: track command workspace changes"
```

---

### Task 4: Version 3 Session Persistence and Drift Recovery

**Files:**
- Modify: `src/coding_agent/session_store.py:9-91`
- Modify: `src/coding_agent/changes.py`
- Modify: `tests/test_session_store.py`
- Modify: `tests/test_changes.py`

**Interfaces:**
- Consumes: `ConversationChangeTracker.serialize()` and `.load_serialized()`.
- Produces: normalized version 3 task keys `file_changes` and `review_path`.
- Produces: `ConversationChangeTracker.retarget(workspace: Path | None) -> None`.
- Guarantees: versions 1 and 2 migrate to version 3 with empty file changes.

- [ ] **Step 1: Replace version expectations with failing version 3 migration tests**

```python
# tests/test_session_store.py
def test_version_two_migrates_to_version_three_with_empty_changes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save({
        "version": 2,
        "current_id": "task-1",
        "projects": [],
        "tasks": [{"id": "task-1", "project_id": None, "title": "Chat", "entries": [], "history": []}],
    })

    state = store.load()

    assert state["version"] == 3
    assert state["tasks"][0]["file_changes"] == []
    assert state["tasks"][0]["review_path"] is None


def test_version_three_skips_only_invalid_change_items(tmp_path: Path) -> None:
    valid = {
        "path": "a.txt", "status": "added", "segments": [],
        "added": 1, "deleted": 0, "binary": False, "truncated": False, "warning": None,
    }
    store = SessionStore(tmp_path)
    store.save({
        "version": 3, "current_id": "task-1", "projects": [],
        "tasks": [{
            "id": "task-1", "project_id": None, "title": "Chat", "entries": [], "history": [],
            "file_changes": [valid, {"path": 42}], "review_path": "a.txt",
        }],
    })
    state = store.load()
    assert state["tasks"][0]["file_changes"] == [valid]
    assert state["tasks"][0]["review_path"] == "a.txt"
```

- [ ] **Step 2: Run session tests and verify version assertion failures**

Run: `python -m pytest tests/test_session_store.py -v`

Expected: FAIL because normalization currently returns version 2 and omits change keys.

- [ ] **Step 3: Implement version 3 normalization and atomic-save regression coverage**

Change `empty_session_state()` to version 3. Use this normalizer for tasks loaded from every supported version:

```python
def _normalize_task(task: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
    task_copy = deepcopy(task)
    raw_changes = task_copy.get("file_changes", [])
    validated_changes = [
        deepcopy(item) for item in raw_changes
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("segments", []), list)
    ] if isinstance(raw_changes, list) else []
    raw_review_path = task_copy.get("review_path")
    task_copy["project_id"] = project_id if project_id is not None else task_copy.get("project_id")
    task_copy["title_is_custom"] = bool(task_copy.get("title_is_custom", False))
    task_copy["file_changes"] = validated_changes
    task_copy["review_path"] = raw_review_path if isinstance(raw_review_path, str) else None
    return task_copy
```

Accept versions 1, 2, and 3 and route every task through `_normalize_task`; always return version 3:

```python
if version == 1:
    # Preserve the current nested-project loop, but append
    # _normalize_task(task, str(project.get("id") or "") or None).
    return {"version": 3, "current_id": None, "projects": normalized_projects, "tasks": normalized_tasks}
if version in {2, 3}:
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        return empty_session_state()
    current_id = value.get("current_id")
    return {
        "version": 3,
        "current_id": current_id if isinstance(current_id, str) else None,
        "projects": [deepcopy(project) for project in projects if isinstance(project, dict)],
        "tasks": [_normalize_task(task) for task in tasks if isinstance(task, dict)],
    }
return empty_session_state()
```

Keep `SessionStore.save()`'s temporary-file replacement and add this failure test:

```python
def test_failed_atomic_replace_preserves_previous_session(monkeypatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    first = {"version": 3, "current_id": None, "projects": [], "tasks": []}
    store.save(first)
    original_replace = Path.replace

    def fail_for_temporary(path: Path, target: Path) -> Path:
        if path.suffix == ".tmp":
            raise OSError("disk failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_for_temporary)
    with pytest.raises(OSError, match="disk failure"):
        store.save({**first, "current_id": "new"})
    assert store.load() == first
```

- [ ] **Step 4: Add drift and workspace-retarget tests**

```python
# tests/test_changes.py
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
```

Implement workspace retargeting without deleting historical segments:

```python
def retarget(self, workspace: Path | None) -> None:
    self.workspace = workspace.resolve() if workspace else None
```

Every `ChangeSegment` already stores `workspace`. `_merge()` starts a new segment when the existing segment's workspace differs from the current resolved workspace, even when both projects contain the same relative path. The file card keeps the relative path label; the Diff pane prints the stored workspace above each segment.

- [ ] **Step 5: Run persistence and tracking tests**

Run: `python -m pytest tests/test_session_store.py tests/test_changes.py -v`

Expected: PASS for migrations, corrupt-item isolation, round trips, drift segments, workspace retargeting, and atomic-save behavior.

- [ ] **Step 6: Commit persistence migration**

```powershell
git add src/coding_agent/session_store.py src/coding_agent/changes.py tests/test_session_store.py tests/test_changes.py
git commit -m "feat: persist conversation diff history"
```

---

### Task 5: Diff Review Widgets and Approved Visual System

**Files:**
- Create: `src/coding_agent/diff_view.py`
- Modify: `src/coding_agent/gui.py:25-75,394-710`
- Modify: `tests/test_gui_design.py`
- Create: `tests/test_diff_view.py`

**Interfaces:**
- Consumes: `FileChange` and `DiffRow` from Task 1.
- Produces: `DiffPalette` and `DiffReviewPane(parent, palette, on_close)`.
- Produces: `DiffReviewPane.show_change(change: FileChange) -> None` and `.clear() -> None`.
- Produces: `FileChangeCard(parent, change, command)` with mouse and Enter activation.

- [ ] **Step 1: Write failing visual-token and row-format tests**

```python
# tests/test_gui_design.py
def test_approved_workspace_palette_is_applied() -> None:
    assert gui.SIDEBAR == "#244A67"
    assert gui.CANVAS == "#F7F5F0"
    assert gui.SURFACE == "#FFFFFF"
    assert gui.SIGNATURE == "#F2A97E"
    assert gui.DIFF_ADDED_BG == "#DDF4E5"
    assert gui.DIFF_ADDED_FG == "#177245"
    assert gui.DIFF_REMOVED_BG == "#FCE1E1"
    assert gui.DIFF_REMOVED_FG == "#B33A3A"
```

```python
# tests/test_diff_view.py
from coding_agent.changes import build_diff_rows


def test_diff_rows_have_old_and_new_line_numbers() -> None:
    rows = build_diff_rows("one\nold\n", "one\nnew\nextra\n")
    removed = next(row for row in rows if row.kind == "removed")
    added = [row for row in rows if row.kind == "added"]
    assert (removed.old_line, removed.new_line, removed.text) == (2, None, "old")
    assert [(row.old_line, row.new_line) for row in added] == [(None, 2), (None, 3)]
```

- [ ] **Step 2: Run focused tests and verify token/module failures**

Run: `python -m pytest tests/test_gui_design.py::test_approved_workspace_palette_is_applied tests/test_diff_view.py -v`

Expected: FAIL because the constants and `diff_view.py` do not exist.

- [ ] **Step 3: Apply global palette and implement the focused widget module**

```python
# src/coding_agent/diff_view.py
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable

from .changes import FileChange, build_diff_rows


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


class DiffReviewPane(tk.Frame):
    def __init__(self, parent: tk.Misc, palette: DiffPalette, on_close: Callable[[], None]) -> None:
        super().__init__(parent, bg=palette.surface)
        self.palette = palette
        self.on_close = on_close
        self.path_label = tk.Label(self, bg=palette.surface, fg=palette.text, font=(palette.mono_font, 10, "bold"))
        self.stats_label = tk.Label(self, bg=palette.surface, fg=palette.muted, font=(palette.mono_font, 9, "bold"))
        self.close_button = tk.Button(self, text="×", command=on_close, relief="flat", bg=palette.surface)
        self.text = tk.Text(self, wrap="none", relief="flat", bg=palette.surface, fg=palette.text,
                            font=(palette.mono_font, 9), state="disabled")
        self.text.tag_configure("added", background=palette.added_bg, foreground=palette.added_fg)
        self.text.tag_configure("removed", background=palette.removed_bg, foreground=palette.removed_fg)
        self.text.tag_configure("hunk", background=palette.canvas, foreground=palette.muted)

    def show_change(self, change: FileChange) -> None:
        self.path_label.configure(text=change.path)
        self.stats_label.configure(text=f"+{change.added}  -{change.deleted}")
        # Render every persisted segment in order using build_diff_rows().

    def clear(self) -> None:
        self.path_label.configure(text="")
        self.stats_label.configure(text="")
```

Use the following rendering and activation structure; add horizontal and vertical `tk.Scrollbar` widgets wired to `xview` and `yview` in `_build()`:

```python
class FileChangeCard(tk.Frame):
    def __init__(self, parent: tk.Misc, change: FileChange, command: Callable[[str], None], palette: DiffPalette) -> None:
        super().__init__(parent, bg=palette.surface, highlightthickness=1,
                         highlightbackground=palette.border, takefocus=True, cursor="hand2")
        self.change = change
        self.command = command
        tk.Label(self, text=change.path, bg=palette.surface, fg=palette.text,
                 font=(palette.mono_font, 9)).pack(side="left", padx=(10, 8), pady=7)
        tk.Label(self, text=f"+{change.added}  -{change.deleted}", bg=palette.surface,
                 fg=palette.muted, font=(palette.mono_font, 9, "bold")).pack(side="right", padx=10)
        self.bind("<Button-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)

    def _activate(self, _event: tk.Event[tk.Misc]) -> str:
        self.command(self.change.path)
        return "break"

def _insert_row(self, row: DiffRow) -> None:
    if row.kind == "hunk":
        rendered = f"     {row.text}\n"
    else:
        old = "" if row.old_line is None else str(row.old_line)
        new = "" if row.new_line is None else str(row.new_line)
        marker = "+" if row.kind == "added" else "-" if row.kind == "removed" else " "
        rendered = f"{old:>5} {new:>5} {marker} {row.text}\n"
    self.text.insert("end", rendered, row.kind)

def show_change(self, change: FileChange) -> None:
    self.path_label.configure(text=change.path)
    self.stats_label.configure(text=f"+{change.added}  -{change.deleted}")
    self.text.configure(state="normal")
    self.text.delete("1.0", "end")
    for segment in change.segments:
        self.text.insert("end", f"工作区：{segment.workspace}\n", "hunk")
        if segment.baseline.text is None or segment.latest.text is None:
            reason = segment.latest.reason or segment.baseline.reason or "没有可用的文本预览"
            self.text.insert("end", reason + "\n", "context")
            continue
        for row in build_diff_rows(segment.baseline.text, segment.latest.text):
            self._insert_row(row)
    self.text.configure(state="disabled")
```

Configure `context`, `added`, `removed`, and `hunk` tags before rendering. Show `change.warning` in a separate wrapping label above the text when present; hide it with `grid_remove()` otherwise.

- [ ] **Step 4: Add widget behavior tests with a Tk availability guard**

```python
# tests/test_diff_view.py
import tkinter as tk
import pytest


def tk_root() -> tk.Tk:
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display is unavailable")
    root.withdraw()
    return root


def test_file_card_activates_with_enter() -> None:
    root = tk_root()
    called: list[str] = []
    try:
        card = FileChangeCard(root, sample_change(), lambda path: called.append(path))
        card.event_generate("<Return>")
        root.update()
        assert called == ["src/app.py"]
    finally:
        root.destroy()
```

- [ ] **Step 5: Run visual and widget tests**

Run: `python -m pytest tests/test_gui_design.py tests/test_diff_view.py -v`

Expected: PASS or explicit Tk-display skips only for tests that instantiate widgets. Contrast-ratio tests must pass with the new derived text colors.

- [ ] **Step 6: Commit the visual system and widgets**

```powershell
git add src/coding_agent/diff_view.py src/coding_agent/gui.py tests/test_diff_view.py tests/test_gui_design.py
git commit -m "feat: add file diff review widgets"
```

---

### Task 6: Per-Conversation GUI Integration and Persistent File Cards

**Files:**
- Modify: `src/coding_agent/gui.py:85-104,394-710,698-790,967-995,1189-1360`
- Modify: `tests/test_gui_sessions.py`
- Modify: `tests/test_gui_design.py`

**Interfaces:**
- Consumes: `ConversationChangeTracker`, `ChangeSet.to_event()`, `DiffReviewPane`, and `FileChangeCard`.
- Produces: `ChatEntry.change_paths: tuple[str, ...]`.
- Produces: `TaskSession.change_tracker: ConversationChangeTracker` and `review_path: str | None`.
- Produces: `CodingAgentApp._open_change(path: str)`, `_close_review()`, and `_sync_review_pane(session)`.

- [ ] **Step 1: Write failing logic-only tests for event consumption and task isolation**

```python
# tests/test_gui_sessions.py
def test_tool_end_adds_clickable_change_paths_to_entry(tmp_path: Path) -> None:
    app, project, task = make_app_with_bound_task()
    task.change_tracker = ConversationChangeTracker(tmp_path)
    app._set_status = lambda *_args: None
    app._render_transcript = lambda *_args: None
    app._save_sessions = lambda: None
    data = {
        "name": "write_file", "ok": True, "output": "ok", "error": None,
        "changes": {"paths": ["a.txt"], "warning": None, "files": []},
    }

    app._handle_agent_event(task.id, "tool_end", data)

    assert task.entries[-1].change_paths == ("a.txt",)


def test_open_change_only_reads_current_task() -> None:
    app, first, second = make_app_with_two_tasks()
    first.change_tracker = tracker_with_change("first.py")
    second.change_tracker = tracker_with_change("second.py")
    shown: list[str] = []
    app.review_pane = SimpleNamespace(show_change=lambda change: shown.append(change.path))
    app._show_review_container = lambda: None

    app.current_id = first.id
    app._open_change("first.py")

    assert shown == ["first.py"]
    assert second.review_path is None
```

- [ ] **Step 2: Run focused GUI tests and verify missing-field failures**

Run: `python -m pytest tests/test_gui_sessions.py -k "tool_end_adds_clickable or open_change_only" -v`

Expected: FAIL because `ChatEntry`, `TaskSession`, and `CodingAgentApp` do not expose the new fields or methods.

- [ ] **Step 3: Add tracker ownership and preserve it across agent retargeting**

Update the dataclasses:

```python
@dataclass(slots=True)
class ChatEntry:
    kind: str
    text: str
    change_paths: tuple[str, ...] = ()


@dataclass(slots=True)
class TaskSession:
    id: str
    project_id: str | None
    title: str
    agent: CodingAgent
    cancel_event: threading.Event
    change_tracker: ConversationChangeTracker = field(default_factory=lambda: ConversationChangeTracker(None))
    entries: list[ChatEntry] = field(default_factory=list)
    running: bool = False
    title_is_custom: bool = False
    review_path: str | None = None
```

Use the tracker explicitly when constructing production agents:

```python
def _make_agent(self, task_id: str, cancel_event: threading.Event, workspace: Path | None,
                change_tracker: ConversationChangeTracker) -> CodingAgent:
    model = OpenAIChatModel(
        api_key=self.config.api_key, model=self.config.model, base_url=self.config.base_url,
        timeout=self.config.request_timeout, max_retries=self.config.max_retries,
    )
    tools = ToolRegistry(
        workspace,
        approver=lambda command, risk, reason: self._request_approval(task_id, command, risk, reason),
        is_cancelled=cancel_event.is_set,
        approval_mode=self.config.approval_mode,
        change_tracker=change_tracker,
    )
    return CodingAgent(
        model, tools, ContextManager(self.config.context_tokens), max_steps=self.config.max_steps,
        on_event=lambda name, data: self.events.put(("agent_event", task_id, name, data)),
        is_cancelled=cancel_event.is_set,
        system_prompt=PROJECTLESS_SYSTEM_PROMPT if workspace is None else SYSTEM_PROMPT,
    )

def _retarget_agent(self, session: TaskSession, project: ProjectSession | None) -> None:
    history = list(session.agent.history)
    workspace = project.path if project else None
    session.change_tracker.retarget(workspace)
    session.agent = self._make_agent(session.id, session.cancel_event, workspace, session.change_tracker)
    session.agent.history = [session.agent.history[0], *history[1:]]
```

In `new_task`, create `tracker = ConversationChangeTracker(None)`, pass it to `_make_agent`, and assign it to `TaskSession.change_tracker`.

- [ ] **Step 4: Build the inner paned layout and event-to-card flow**

Inside `_build_layout`, replace the direct content transcript parent with an internal horizontal `tk.PanedWindow` containing:

1. The existing chat frame.
2. A lazily added review container holding `DiffReviewPane`.

Keep the outer sidebar split unchanged. `_handle_agent_event(..., "tool_end", data)` must append:

```python
change_paths = tuple(
    path for path in data.get("changes", {}).get("paths", []) if isinstance(path, str)
)
session.entries.append(
    ChatEntry("tool" if data["ok"] else "error", rendered_text, change_paths=change_paths)
)
```

During `_render_transcript`, render the text first, then create one `FileChangeCard` per still-valid path and insert it with `self.transcript.window_create("end", window=card)`. The card command must capture the path by value (`lambda selected=path: self._open_change(selected)`).

- [ ] **Step 5: Persist and restore changes, entry paths, and review selection**

Save version 3 tasks with:

```python
{
    "id": task.id,
    "project_id": task.project_id,
    "title": task.title,
    "title_is_custom": task.title_is_custom,
    "entries": [
        {"kind": entry.kind, "text": entry.text, "change_paths": list(entry.change_paths)}
        for entry in task.entries
    ],
    "history": task.agent.history,
    "file_changes": task.change_tracker.serialize(),
    "review_path": task.review_path,
}
```

In `_load_sessions`, create a tracker for the resolved project workspace, call `load_serialized`, then pass that tracker into `_make_agent` and `TaskSession`. Filter `change_paths` to strings. If `review_path` is not present in restored changes, set it to `None`.

- [ ] **Step 6: Add persistence and pane-state assertions**

Replace the old version-2 save assertion with this concrete shape check:

```python
CodingAgentApp._save_sessions(app)
payload = saved[0]
assert payload["version"] == 3
assert payload["tasks"][0]["file_changes"] == task.change_tracker.serialize()
assert payload["tasks"][0]["entries"][0]["change_paths"] == ["a.txt"]
assert payload["tasks"][0]["review_path"] == "a.txt"
```

Add a load test whose store returns two tasks with distinct serialized changes:

```python
app.store = SimpleNamespace(load=lambda: {
    "version": 3,
    "current_id": "task-1",
    "projects": [{"id": "p1", "title": "Demo", "path": str(tmp_path)}],
    "tasks": [
        {"id": "task-1", "project_id": "p1", "title": "First", "entries": [], "history": [],
         "file_changes": serialized_change("first.py"), "review_path": "first.py"},
        {"id": "task-2", "project_id": "p1", "title": "Second", "entries": [], "history": [],
         "file_changes": serialized_change("second.py"), "review_path": "second.py"},
    ],
})
app._load_sessions()

assert app.tasks[0].change_tracker is not app.tasks[1].change_tracker
assert app.tasks[0].review_path == "first.py"
assert app.tasks[1].review_path == "second.py"
```

Use this helper above the fixture so tests do not handcraft private serialization fields:

```python
def serialized_change(workspace: Path, relative: str) -> list[dict[str, object]]:
    tracker = ConversationChangeTracker(workspace)
    capture = tracker.capture_paths([relative])
    (workspace / relative).write_text("changed\n", encoding="utf-8")
    tracker.finish(capture)
    return tracker.serialize()
```

Call it as `serialized_change(tmp_path, "first.py")` and `serialized_change(tmp_path, "second.py")` in the store fixture.

- [ ] **Step 7: Run GUI and persistence suites**

Run: `python -m pytest tests/test_gui_sessions.py tests/test_gui_design.py tests/test_session_store.py tests/test_diff_view.py -v`

Expected: PASS, including existing projectless conversations, directory binding, project removal, rename targeting, Enter behavior, and new Diff state.

- [ ] **Step 8: Commit GUI integration**

```powershell
git add src/coding_agent/gui.py tests/test_gui_sessions.py tests/test_gui_design.py
git commit -m "feat: integrate per-conversation diff review"
```

---

### Task 7: End-to-End Regression, Security Boundary, and Manual UI Verification

**Files:**
- Modify: `tests/test_agent.py`
- Modify: `tests/test_cli_and_boundaries.py`
- Modify: `tests/test_gui_sessions.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: complete automated acceptance coverage and repository hygiene for visual-companion artifacts.

- [ ] **Step 1: Write the scripted-agent cumulative workflow test**

```python
# tests/test_agent.py
def test_agent_retry_workflow_keeps_final_cumulative_diff(tmp_path: Path) -> None:
    tracker = ConversationChangeTracker(tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []
    responses = [
        AssistantResponse(tool_calls=[call("c1", "write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"})]),
        AssistantResponse(tool_calls=[call("c2", "run_command", {"command": "python -c \"import sys; sys.exit(1)\"", "timeout_seconds": 10})]),
        AssistantResponse(tool_calls=[call("c3", "replace_text", {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"})]),
        AssistantResponse(tool_calls=[call("c4", "run_command", {"command": "python -c \"from calc import add; assert add(2, 3) == 5\"", "timeout_seconds": 10})]),
        AssistantResponse("修复完成"),
    ]
    model = ScriptedModel(responses)
    tools = ToolRegistry(tmp_path, approver=lambda *_args: True, change_tracker=tracker)
    agent = CodingAgent(model, tools, ContextManager(100_000), on_event=lambda name, data: events.append((name, data)))

    assert agent.run("实现并测试加法") == "修复完成"
    assert tracker.changes["calc.py"].segments[0].latest.text == "def add(a, b):\n    return a + b\n"
    assert any(data.get("changes", {}).get("paths") == ["calc.py"] for name, data in events if name == "tool_end")
```

- [ ] **Step 2: Add repository-boundary assertions**

```python
# tests/test_cli_and_boundaries.py
def test_agent_frameworks_and_hosted_tools_are_not_dependencies() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8").lower()
    for forbidden in ("openai-agents", "langchain", "llama-index", "autogen", "crewai"):
        assert forbidden not in pyproject


def test_visual_companion_state_is_git_ignored() -> None:
    patterns = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".superpowers/" in patterns
```

- [ ] **Step 3: Run the new end-to-end and boundary tests**

Run: `python -m pytest tests/test_agent.py::test_agent_retry_workflow_keeps_final_cumulative_diff tests/test_cli_and_boundaries.py -v`

Expected: the workflow test passes; the ignore test initially fails until `.superpowers/` is added.

- [ ] **Step 4: Add `.superpowers/` to `.gitignore` without touching credentials**

Append exactly this repository-local ignore entry:

```gitignore
.superpowers/
```

Do not add API keys, local setting contents, or generated preview HTML to Git.

- [ ] **Step 5: Run the complete automated suite**

Run: `python -m pytest -q`

Expected: all tests pass; no test contacts a real API. Record the exact pass count and any platform-specific Tk or symlink skips.

- [ ] **Step 6: Inspect dependency and credential boundaries**

Run: `python -m pip show openai openai-agents langchain llama-index autogen crewai`

Expected: `openai` may be installed; forbidden Agent frameworks are not project requirements. A package installed globally is not a project failure, so also inspect `pyproject.toml` as the authoritative dependency list.

Run: `git grep -n -I -E "sk-[A-Za-z0-9_-]{16,}|CODING_AGENT_API_KEY[[:space:]]*=" -- . ':!docs/superpowers/plans/2026-08-28-conversation-file-diff-review.md'`

Expected: no real credential assignments or key-shaped values in tracked project files.

- [ ] **Step 7: Perform manual desktop acceptance**

Run: `python -m coding_agent`

Verify at 1240×800 and 960×640:

1. Create or select a disposable workspace and start a new conversation.
2. Ask the Agent to create one text file, modify another, run a formatting or generation command, and delete a third.
3. Confirm file cards appear under tool records with correct status and `+N / -N`.
4. Click a card and confirm the right pane opens while chat and project navigation remain visible.
5. Confirm red deleted lines, green added lines, old/new line numbers, `@@` headers, dragging, file switching, keyboard activation, and close behavior.
6. Restart the application and confirm historical cards and Diff snapshots reopen.
7. Modify a tracked file manually, continue the conversation, and confirm the old segment is marked as external drift while later Agent changes form a new segment.
8. Confirm the full application uses the approved blue-gray, paper-white, peach, green, and red visual system.

- [ ] **Step 8: Review the final diff and commit acceptance coverage**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `git status --short`

Expected: only intended source, test, `.gitignore`, spec, and plan changes are present; `.superpowers/` is ignored.

```powershell
git add .gitignore tests/test_agent.py tests/test_cli_and_boundaries.py tests/test_gui_sessions.py
git commit -m "test: verify persistent diff review workflow"
```

After the commit, run `python -m pytest -q` once more and report the exact result before claiming completion.
