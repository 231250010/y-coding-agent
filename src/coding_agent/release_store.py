from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def empty_release_state(workspace: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "workspace": str(workspace.resolve()),
        "active": {},
        "releases": [],
        "rollback_plans": [],
        "rollback_events": [],
    }


class ReleaseStore:
    """Atomic, local-only persistence for Compose release and rollback audit data."""

    def __init__(self, workspace: Path, state_root: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        if state_root is None:
            self.path = self.workspace / ".coding-agent" / "releases.json"
        else:
            identity = hashlib.sha256(str(self.workspace).casefold().encode("utf-8")).hexdigest()[:20]
            self.path = state_root.resolve() / f"{identity}.json"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return empty_release_state(self.workspace)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"发布记录无法读取: {exc}") from exc
        if not isinstance(loaded, dict) or loaded.get("version") != 1:
            raise ValueError("发布记录格式或版本无效")
        if loaded.get("workspace") != str(self.workspace):
            raise ValueError("发布记录不属于当前工作区")
        state = empty_release_state(self.workspace)
        active = loaded.get("active")
        if isinstance(active, dict):
            state["active"] = {
                str(key): str(value)
                for key, value in active.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        for key in ("releases", "rollback_plans", "rollback_events"):
            value = loaded.get(key)
            if isinstance(value, list):
                state[key] = [deepcopy(item) for item in value if isinstance(item, dict)]
        return state

    def save(self, state: dict[str, Any]) -> None:
        payload = deepcopy(state)
        payload["version"] = 1
        payload["workspace"] = str(self.workspace)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)
