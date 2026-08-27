from pathlib import Path

from coding_agent.session_store import SessionStore


def test_session_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    value = {
        "version": 1,
        "projects": [
            {
                "id": "project-1",
                "path": str(tmp_path),
                "tasks": [{"id": "task-1", "title": "修复测试", "entries": [], "history": []}],
            }
        ],
    }
    store.save(value)
    assert store.load() == value
    assert "api_key" not in store.path.read_text(encoding="utf-8")


def test_session_store_recovers_from_invalid_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")
    assert store.load() == {"version": 1, "projects": []}
