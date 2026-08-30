from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.release_store import ReleaseStore, empty_release_state


def test_release_store_is_workspace_scoped_and_atomic(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    root = tmp_path / "local-state"
    store = ReleaseStore(workspace, root)
    state = empty_release_state(workspace)
    state["active"]["production"] = "v1"

    store.save(state)

    assert store.path.parent == root.resolve()
    assert store.path.suffix == ".json"
    assert store.load()["active"] == {"production": "v1"}
    assert not store.path.with_suffix(".tmp").exists()


def test_corrupt_release_store_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = ReleaseStore(workspace, tmp_path / "state")
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="无法读取"):
        store.load()
