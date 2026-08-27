from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def empty_session_state() -> dict[str, Any]:
    return {"version": 2, "current_id": None, "projects": [], "tasks": []}


def normalize_session_state(value: Any) -> dict[str, Any]:
    """Return a detached version-2 representation of a stored session."""
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
                task_copy = deepcopy(task)
                task_copy["project_id"] = project.get("id")
                task_copy["title_is_custom"] = False
                normalized_tasks.append(task_copy)
        return {
            "version": 2,
            "current_id": None,
            "projects": normalized_projects,
            "tasks": normalized_tasks,
        }

    if version == 2:
        tasks = value.get("tasks")
        if not isinstance(tasks, list):
            return empty_session_state()
        normalized_tasks = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_copy = deepcopy(task)
            task_copy["project_id"] = task_copy.get("project_id")
            task_copy["title_is_custom"] = bool(task_copy.get("title_is_custom", False))
            normalized_tasks.append(task_copy)
        current_id = value.get("current_id")
        return {
            "version": 2,
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
