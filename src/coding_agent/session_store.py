from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def empty_session_state() -> dict[str, Any]:
    return {"version": 5, "current_id": None, "projects": [], "tasks": []}


def _normalize_task(task: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
    task_copy = deepcopy(task)
    raw_changes = task_copy.get("file_changes", [])
    if isinstance(raw_changes, list):
        validated_changes = [
            deepcopy(item)
            for item in raw_changes
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("segments", []), list)
        ]
    else:
        validated_changes = []
    raw_review_path = task_copy.get("review_path")
    task_copy["project_id"] = project_id if project_id is not None else task_copy.get("project_id")
    task_copy["title_is_custom"] = bool(task_copy.get("title_is_custom", False))
    task_copy["file_changes"] = validated_changes
    task_copy["review_path"] = raw_review_path if isinstance(raw_review_path, str) else None
    raw_worktree = task_copy.get("worktree")
    if isinstance(raw_worktree, dict):
        task_copy["worktree"] = deepcopy(raw_worktree)
    else:
        task_copy.pop("worktree", None)
    raw_task_list = task_copy.get("task_list")
    if isinstance(raw_task_list, dict):
        task_copy["task_list"] = deepcopy(raw_task_list)
    else:
        task_copy.pop("task_list", None)
    return task_copy


def normalize_session_state(value: Any) -> dict[str, Any]:
    """Return a detached current-version representation of a stored session."""
    if not isinstance(value, dict):
        return empty_session_state()

    version = value.get("version")
    projects = value.get("projects")
    if not isinstance(projects, list):
        return empty_session_state()

    if version == 1:
        normalized_projects: list[dict[str, Any]] = []
        normalized_tasks: list[dict[str, Any]] = []
        for project in projects:
            if not isinstance(project, dict):
                continue
            project_copy = deepcopy(project)
            project_copy.pop("tasks", None)
            normalized_projects.append(project_copy)
            nested_tasks = project.get("tasks")
            if not isinstance(nested_tasks, list):
                continue
            for task in nested_tasks:
                if not isinstance(task, dict):
                    continue
                project_id = project.get("id")
                normalized_tasks.append(
                    _normalize_task(task, project_id if isinstance(project_id, str) else None)
                )
        return {
            "version": 5,
            "current_id": None,
            "projects": normalized_projects,
            "tasks": normalized_tasks,
        }

    if version in {2, 3, 4, 5}:
        tasks = value.get("tasks")
        if not isinstance(tasks, list):
            return empty_session_state()
        normalized_tasks = [_normalize_task(task) for task in tasks if isinstance(task, dict)]
        current_id = value.get("current_id")
        return {
            "version": 5,
            "current_id": current_id if isinstance(current_id, str) else None,
            "projects": [deepcopy(project) for project in projects if isinstance(project, dict)],
            "tasks": normalized_tasks,
        }

    return empty_session_state()


class SessionStore:
    """Stores project/task UI state in a Git-ignored local JSON file."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".coding-agent" / "sessions.json"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return empty_session_state()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty_session_state()
        return normalize_session_state(value)

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
