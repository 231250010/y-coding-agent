from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SessionStore:
    """Stores project/task UI state in a Git-ignored local JSON file."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".coding-agent" / "sessions.json"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "projects": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "projects": []}
        if not isinstance(value, dict) or not isinstance(value.get("projects"), list):
            return {"version": 1, "projects": []}
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
