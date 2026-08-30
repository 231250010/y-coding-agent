from pathlib import Path

import pytest

from coding_agent.session_store import SessionStore


def test_session_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    value = {
        "version": 4,
        "current_id": "task-1",
        "projects": [],
        "tasks": [{
            "id": "task-1", "project_id": None, "title": "修复测试",
            "title_is_custom": True, "entries": [], "history": [],
            "file_changes": [], "review_path": None,
        }],
    }
    store.save(value)
    assert store.load() == value
    assert "api_key" not in store.path.read_text(encoding="utf-8")


def test_session_store_recovers_from_invalid_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")
    assert store.load() == {"version": 4, "current_id": None, "projects": [], "tasks": []}


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

    assert state["version"] == 4
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

    state = store.load()
    assert state["version"] == 4
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


def test_failed_atomic_replace_preserves_previous_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    first = {"version": 4, "current_id": None, "projects": [], "tasks": []}
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
