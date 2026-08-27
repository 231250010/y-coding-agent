from pathlib import Path

from coding_agent.session_store import SessionStore


def test_session_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    value = {
        "version": 2,
        "current_id": "task-1",
        "projects": [],
        "tasks": [{
            "id": "task-1", "project_id": None, "title": "修复测试",
            "title_is_custom": True, "entries": [], "history": [],
        }],
    }
    store.save(value)
    assert store.load() == value
    assert "api_key" not in store.path.read_text(encoding="utf-8")


def test_session_store_recovers_from_invalid_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")
    assert store.load() == {"version": 2, "current_id": None, "projects": [], "tasks": []}


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
