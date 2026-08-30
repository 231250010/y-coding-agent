from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from copy import deepcopy
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any


class ReleaseLockBusy(RuntimeError):
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        super().__init__("发布状态正被另一个进程使用")
        self.metadata = metadata or {}


class InterProcessFileLock(AbstractContextManager["InterProcessFileLock"]):
    """Non-blocking advisory lock whose ownership follows the open file descriptor."""

    def __init__(self, path: Path, metadata: dict[str, Any]) -> None:
        self.path = path
        self.owner_path = path.with_name(path.name + ".owner.json")
        self.metadata = {
            **metadata,
            "pid": os.getpid(),
            "acquired_at": time.time(),
            "token": uuid.uuid4().hex,
        }
        self._handle: Any = None

    def __enter__(self) -> "InterProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            self._lock(handle)
        except OSError as exc:
            handle.close()
            raise ReleaseLockBusy(self.read_metadata(self.path)) from exc
        try:
            self._write_metadata()
        except Exception:
            handle.seek(0)
            self._unlock(handle)
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            owner = self.read_metadata(self.path)
            if owner.get("token") == self.metadata["token"]:
                try:
                    self.owner_path.unlink(missing_ok=True)
                except OSError:
                    # Owner metadata is observational; closing the descriptor still releases the lock.
                    pass
            handle.seek(0)
            try:
                self._unlock(handle)
            except OSError:
                # close() below is the authoritative fallback for releasing advisory locks.
                pass
        finally:
            handle.close()

    @staticmethod
    def _lock(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def read_metadata(path: Path) -> dict[str, Any]:
        owner_path = path.with_name(path.name + ".owner.json")
        try:
            loaded = json.loads(owner_path.read_text(encoding="utf-8")[:4096])
        except (OSError, UnicodeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write_metadata(self) -> None:
        temporary = self.owner_path.with_name(
            f".{self.owner_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self.metadata, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(temporary, self.owner_path)
        finally:
            temporary.unlink(missing_ok=True)


def empty_release_state(workspace: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "revision": 0,
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
        revision = loaded.get("revision", 0)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("发布记录修订号无效")
        state["revision"] = revision
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
        revision = payload.get("revision", 0)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("发布记录修订号无效")
        payload["revision"] = revision + 1
        state["revision"] = payload["revision"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def transaction(
        self, operation: str, environment: str
    ) -> InterProcessFileLock:
        return InterProcessFileLock(
            self.path.with_suffix(".transaction.lock"),
            {
                "kind": "release_transaction",
                "operation": operation,
                "environment": environment,
                "workspace": str(self.workspace),
            },
        )

    def environment_lock(
        self, identity: str, operation: str, environment: str
    ) -> InterProcessFileLock:
        path = self.environment_lock_path(identity)
        return InterProcessFileLock(
            path,
            {
                "kind": "environment_operation",
                "operation": operation,
                "environment": environment,
                "workspace": str(self.workspace),
            },
        )

    def environment_lock_path(self, identity: str) -> Path:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return self.path.parent / ".locks" / f"{self.path.stem}-{digest}.lock"

    def environment_owner(self, identity: str) -> dict[str, Any]:
        path = self.environment_lock_path(identity)
        try:
            with InterProcessFileLock(
                path,
                {"kind": "lock_probe", "workspace": str(self.workspace)},
            ):
                return {}
        except ReleaseLockBusy as exc:
            return exc.metadata
