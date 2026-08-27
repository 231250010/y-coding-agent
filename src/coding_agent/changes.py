from __future__ import annotations

import difflib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


MAX_TEXT_BYTES = 1_048_576
MAX_COMMAND_SNAPSHOT_BYTES = 32 * 1_048_576
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".worktrees",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coding-agent",
        ".superpowers",
    }
)


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    exists: bool
    size: int = 0
    digest: str | None = None
    text: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DiffRow:
    kind: str
    old_line: int | None
    new_line: int | None
    text: str


@dataclass(slots=True)
class ChangeSegment:
    workspace: str
    baseline: FileSnapshot
    latest: FileSnapshot
    drifted: bool = False


@dataclass(slots=True)
class FileChange:
    path: str
    segments: list[ChangeSegment]
    status: str
    added: int
    deleted: int
    binary: bool = False
    truncated: bool = False
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    paths: tuple[str, ...] = ()
    warning: str | None = None

    def to_event(self, changes: dict[str, FileChange]) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "warning": self.warning,
            "files": [file_change_to_dict(changes[path]) for path in self.paths if path in changes],
        }


@dataclass(frozen=True, slots=True)
class Capture:
    snapshots: dict[str, FileSnapshot]
    warning: str | None = None
    workspace_scan: bool = False


def line_counts(old: str, new: str) -> tuple[int, int]:
    added = deleted = 0
    matcher = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines(), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += old_end - old_start
        if tag in {"replace", "insert"}:
            added += new_end - new_start
    return added, deleted


def build_diff_rows(old: str, new: str, context: int = 3) -> list[DiffRow]:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[DiffRow] = []
    for group in matcher.get_grouped_opcodes(context):
        first = group[0]
        last = group[-1]
        old_start = first[1] + 1
        new_start = first[3] + 1
        old_count = last[2] - first[1]
        new_count = last[4] - first[3]
        rows.append(DiffRow("hunk", None, None, f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"))
        for tag, old_from, old_to, new_from, new_to in group:
            if tag == "equal":
                rows.extend(
                    DiffRow("context", index + 1, new_from + index - old_from + 1, old_lines[index])
                    for index in range(old_from, old_to)
                )
            elif tag in {"replace", "delete"}:
                rows.extend(
                    DiffRow("removed", index + 1, None, old_lines[index])
                    for index in range(old_from, old_to)
                )
            if tag in {"replace", "insert"}:
                rows.extend(
                    DiffRow("added", None, index + 1, new_lines[index])
                    for index in range(new_from, new_to)
                )
    return rows


def _snapshot_to_dict(snapshot: FileSnapshot) -> dict[str, Any]:
    return {
        "exists": snapshot.exists,
        "size": snapshot.size,
        "digest": snapshot.digest,
        "text": snapshot.text,
        "reason": snapshot.reason,
    }


def _snapshot_from_dict(value: Any) -> FileSnapshot | None:
    if not isinstance(value, dict) or not isinstance(value.get("exists"), bool):
        return None
    size = value.get("size", 0)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    digest = value.get("digest")
    text = value.get("text")
    reason = value.get("reason")
    if any(item is not None and not isinstance(item, str) for item in (digest, text, reason)):
        return None
    return FileSnapshot(value["exists"], size, digest, text, reason)


def file_change_to_dict(change: FileChange) -> dict[str, Any]:
    return {
        "path": change.path,
        "segments": [
            {
                "workspace": segment.workspace,
                "baseline": _snapshot_to_dict(segment.baseline),
                "latest": _snapshot_to_dict(segment.latest),
                "drifted": segment.drifted,
            }
            for segment in change.segments
        ],
        "status": change.status,
        "added": change.added,
        "deleted": change.deleted,
        "binary": change.binary,
        "truncated": change.truncated,
        "warning": change.warning,
    }


def file_change_from_dict(value: Any) -> FileChange | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    raw_segments = value.get("segments")
    if not _valid_relative_path(path) or not isinstance(raw_segments, list):
        return None
    segments: list[ChangeSegment] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict) or not isinstance(raw_segment.get("workspace"), str):
            continue
        baseline = _snapshot_from_dict(raw_segment.get("baseline"))
        latest = _snapshot_from_dict(raw_segment.get("latest"))
        if baseline is None or latest is None:
            continue
        segments.append(
            ChangeSegment(
                raw_segment["workspace"],
                baseline,
                latest,
                bool(raw_segment.get("drifted", False)),
            )
        )
    if not segments:
        return None
    counts = [line_counts(segment.baseline.text or "", segment.latest.text or "") for segment in segments]
    warning = value.get("warning")
    return FileChange(
        path=path,
        segments=segments,
        status=str(value.get("status", "modified")),
        added=sum(added for added, _deleted in counts),
        deleted=sum(deleted for _added, deleted in counts),
        binary=bool(value.get("binary", False)),
        truncated=bool(value.get("truncated", False)),
        warning=warning if isinstance(warning, str) else None,
    )


def _valid_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts


class ConversationChangeTracker:
    def __init__(
        self,
        workspace: Path | None,
        *,
        max_text_bytes: int = MAX_TEXT_BYTES,
        max_command_bytes: int = MAX_COMMAND_SNAPSHOT_BYTES,
    ) -> None:
        self.workspace = workspace.resolve() if workspace else None
        self.max_text_bytes = max_text_bytes
        self.max_command_bytes = max_command_bytes
        self.changes: dict[str, FileChange] = {}

    def retarget(self, workspace: Path | None) -> None:
        self.workspace = workspace.resolve() if workspace else None

    def capture_paths(self, paths: Sequence[str]) -> Capture:
        snapshots: dict[str, FileSnapshot] = {}
        for path in paths:
            relative = self._relative(path)
            snapshots[relative] = self._snapshot(relative)
        return Capture(snapshots)

    def capture_workspace(self) -> Capture:
        if self.workspace is None:
            return Capture({}, workspace_scan=True)
        snapshots, skipped = self._workspace_snapshots()
        warning = f"预览不完整：{skipped} 个文件未保存文本快照" if skipped else None
        return Capture(snapshots, warning, workspace_scan=True)

    def finish(self, capture: Capture) -> ChangeSet:
        after_snapshots: dict[str, FileSnapshot]
        warning = capture.warning
        if capture.workspace_scan:
            after_snapshots, skipped = self._workspace_snapshots()
            if skipped:
                after_warning = f"预览不完整：{skipped} 个文件未保存文本快照"
                warning = "；".join(dict.fromkeys(filter(None, (warning, after_warning))))
        else:
            after_snapshots = {path: self._snapshot(path) for path in capture.snapshots}
        changed: list[str] = []
        for path in sorted(set(capture.snapshots) | set(after_snapshots)):
            before = capture.snapshots.get(path, FileSnapshot(False))
            after = after_snapshots.get(path, FileSnapshot(False))
            if self._same(before, after):
                continue
            self._merge(path, before, after)
            changed.append(path)
        return ChangeSet(tuple(changed), warning)

    def serialize(self) -> list[dict[str, Any]]:
        return [file_change_to_dict(self.changes[path]) for path in sorted(self.changes)]

    def load_serialized(self, items: Any) -> None:
        self.changes.clear()
        if not isinstance(items, list):
            return
        for item in items:
            change = file_change_from_dict(item)
            if change is not None:
                self.changes[change.path] = change

    def _relative(self, user_path: str) -> str:
        if self.workspace is None:
            raise ValueError("当前对话尚未选择工作目录")
        raw = Path(user_path)
        candidate = raw if raw.is_absolute() else self.workspace / raw
        resolved = candidate.resolve(strict=False)
        try:
            return resolved.relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise ValueError(f"路径超出工作区: {user_path}") from exc

    def _snapshot(self, relative: str, *, content_budget: int | None = None) -> FileSnapshot:
        if self.workspace is None:
            return FileSnapshot(False, reason="当前对话尚未选择工作目录")
        path = (self.workspace / relative).resolve(strict=False)
        try:
            path.relative_to(self.workspace)
        except ValueError:
            return FileSnapshot(False, reason="路径超出工作区")
        if not path.exists():
            return FileSnapshot(False)
        if not path.is_file():
            return FileSnapshot(True, reason="不是普通文件")

        limit = self.max_text_bytes
        if content_budget is not None:
            limit = min(limit, max(0, content_budget))

        for attempt in range(2):
            before_stat = path.stat()
            preview = path.read_bytes() if before_stat.st_size <= limit else None
            digest = self._hash_file(path)
            after_stat = path.stat()
            if (before_stat.st_size, before_stat.st_mtime_ns) == (
                after_stat.st_size,
                after_stat.st_mtime_ns,
            ):
                break
            if attempt == 1:
                return FileSnapshot(True, after_stat.st_size, reason="扫描期间文件持续变化")
        size = after_stat.st_size
        if preview is None:
            reason = (
                "总快照容量已用尽"
                if content_budget is not None and content_budget < size
                else "文件超过文本预览上限"
            )
            return FileSnapshot(True, size, digest, reason=reason)
        if b"\x00" in preview[:8192]:
            return FileSnapshot(True, size, digest, reason="二进制文件")
        try:
            text = preview.decode("utf-8")
        except UnicodeDecodeError:
            return FileSnapshot(True, size, digest, reason="不是 UTF-8 文本")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return FileSnapshot(True, size, digest, text=text)

    def _workspace_snapshots(self) -> tuple[dict[str, FileSnapshot], int]:
        if self.workspace is None:
            return {}, 0
        snapshots: dict[str, FileSnapshot] = {}
        consumed = 0
        skipped = 0
        for root, directories, filenames in os.walk(self.workspace, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in IGNORED_DIRECTORY_NAMES
            )
            root_path = Path(root)
            for filename in sorted(filenames):
                path = root_path / filename
                try:
                    relative = path.relative_to(self.workspace).as_posix()
                    remaining = max(0, self.max_command_bytes - consumed)
                    snapshot = self._snapshot(relative, content_budget=remaining)
                except (OSError, UnicodeError, ValueError):
                    continue
                snapshots[relative] = snapshot
                if snapshot.text is not None:
                    consumed += len(snapshot.text.encode("utf-8"))
                elif snapshot.reason == "总快照容量已用尽":
                    skipped += 1
        return snapshots, skipped

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _same(left: FileSnapshot, right: FileSnapshot) -> bool:
        return left.exists == right.exists and left.digest == right.digest

    def _merge(self, path: str, before: FileSnapshot, after: FileSnapshot) -> None:
        workspace = str(self.workspace) if self.workspace else ""
        existing = self.changes.get(path)
        if existing is None:
            existing = FileChange(path, [ChangeSegment(workspace, before, after)], "modified", 0, 0)
            self.changes[path] = existing
        else:
            active = existing.segments[-1]
            if active.workspace != workspace or not self._same(active.latest, before):
                active.drifted = True
                existing.segments.append(ChangeSegment(workspace, before, after))
            else:
                active.latest = after

        active = existing.segments[-1]
        if self._same(active.baseline, active.latest):
            existing.segments.pop()
            if not existing.segments:
                self.changes.pop(path, None)
                return
            active = existing.segments[-1]

        existing.status = (
            "added"
            if not active.baseline.exists and active.latest.exists
            else "deleted"
            if active.baseline.exists and not active.latest.exists
            else "modified"
        )
        counts = [
            line_counts(segment.baseline.text or "", segment.latest.text or "")
            for segment in existing.segments
        ]
        existing.added = sum(added for added, _deleted in counts)
        existing.deleted = sum(deleted for _added, deleted in counts)
        reasons = [
            snapshot.reason
            for segment in existing.segments
            for snapshot in (segment.baseline, segment.latest)
            if snapshot.reason
        ]
        existing.binary = any(reason == "二进制文件" for reason in reasons)
        existing.truncated = any(
            snapshot.exists and snapshot.text is None
            for segment in existing.segments
            for snapshot in (segment.baseline, segment.latest)
        ) and not existing.binary
        existing.warning = "；".join(dict.fromkeys(reasons)) or None
