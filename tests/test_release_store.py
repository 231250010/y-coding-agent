from __future__ import annotations

import json
import subprocess
import sys
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
    assert store.load()["revision"] == 1
    assert not store.path.with_suffix(".tmp").exists()
    assert not list(store.path.parent.glob(f".{store.path.name}.*.tmp"))


def test_corrupt_release_store_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = ReleaseStore(workspace, tmp_path / "state")
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="无法读取"):
        store.load()


def test_transaction_lock_reports_other_process_owner_and_releases(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = ReleaseStore(workspace, tmp_path / "state")
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = """
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from coding_agent.release_store import InterProcessFileLock, ReleaseLockBusy
try:
    with InterProcessFileLock(Path(sys.argv[2]), {"operation": "child"}):
        print("acquired")
except ReleaseLockBusy as exc:
    print(json.dumps(exc.metadata))
    raise SystemExit(23)
"""

    with store.transaction("compose_release", "production") as held:
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(source_root), str(held.path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    acquired = subprocess.run(
        [sys.executable, "-c", script, str(source_root), str(held.path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert blocked.returncode == 23
    owner = json.loads(blocked.stdout)
    assert owner["operation"] == "compose_release"
    assert owner["environment"] == "production"
    assert isinstance(owner["pid"], int)
    assert acquired.returncode == 0
    assert acquired.stdout.strip() == "acquired"


def test_environment_locks_are_isolated_by_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = ReleaseStore(workspace, tmp_path / "state")

    first = store.environment_lock("workspace|current|staging", "deploy", "staging")
    second = store.environment_lock("workspace|current|production", "deploy", "production")

    assert first.path != second.path
    with first, second:
        assert first.path.is_file()
        assert second.path.is_file()
        owner = store.environment_owner("workspace|current|staging")
        assert owner["operation"] == "deploy"
        assert owner["environment"] == "staging"

    assert store.environment_owner("workspace|current|staging") == {}
