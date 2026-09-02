from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent import AgentCancelled, AgentStopped, CodingAgent
from .changes import (
    ConversationChangeTracker,
    FileChange,
    build_diff_rows,
    file_change_from_dict,
    file_change_to_dict,
)
from .config import Config
from .context import ContextManager
from .execution_state import ExecutionState
from .local_settings import LocalSettings
from .model import ChatModel, ModelError, OpenAIChatModel
from .permissions import PERMISSION_MODES, normalize_permission_mode
from .project_policy import load_project_policy
from .prompts import PROJECTLESS_SYSTEM_PROMPT, SYSTEM_PROMPT, with_project_rules
from .safety import RiskLevel
from .session_store import SessionStore
from .task_list import TaskListState
from .providers import build_default_tool_provider


MAX_DECISION_SUMMARY_CHARS = 500
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|credential)\b"
    r"(\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,，;；]+)"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)\bauthorization\b(\s*[=:]\s*)(?:bearer\s+)?[^\s,，;；]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


class RuntimeNotFound(LookupError):
    """Raised when a requested project, conversation, approval, or change does not exist."""


class RuntimeConflict(RuntimeError):
    """Raised when an operation conflicts with a running conversation."""


@dataclass(slots=True)
class ChatEntry:
    kind: str
    text: str
    change_paths: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    file_changes: list[dict[str, Any]] = field(default_factory=list)
    step: int | None = None
    tools: tuple[str, ...] = ()


@dataclass(slots=True)
class WebProject:
    id: str
    title: str
    path: Path


@dataclass(slots=True)
class PendingApproval:
    id: str
    task_id: str
    command: str
    risk: str
    reason: str
    signal: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


@dataclass(slots=True)
class WebTask:
    id: str
    project_id: str | None
    permission_mode: str = "risk"
    title: str = "新对话"
    title_is_custom: bool = False
    entries: list[ChatEntry] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    running: bool = False
    status: str = "就绪"
    streaming_content: str = ""
    progress: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    change_tracker: ConversationChangeTracker = field(
        default_factory=lambda: ConversationChangeTracker(None)
    )
    pending_change_paths: list[str] = field(default_factory=list)
    review_path: str | None = None
    worktree_path: Path | None = None
    worktree_root: Path | None = None
    worktree_branch: str | None = None
    worktree_base_branch: str | None = None
    worktree_base_commit: str | None = None
    workspace_changing: bool = False
    task_list: TaskListState = field(default_factory=TaskListState)
    execution_state: ExecutionState = field(default_factory=ExecutionState)


ModelFactory = Callable[[], ChatModel]


class WebRuntime:
    """Thread-safe application state used by the local HTTP frontend."""

    def __init__(
        self,
        settings: LocalSettings,
        settings_root: Path,
        *,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.settings = settings
        self.settings_root = settings_root.resolve()
        self.store = SessionStore(self.settings_root)
        self.model_factory = model_factory
        self.projects: list[WebProject] = []
        self.tasks: list[WebTask] = []
        self.current_id: str | None = None
        self.approvals: dict[str, PendingApproval] = {}
        self.lock = threading.RLock()
        self.events = threading.Condition(self.lock)
        self.revision = 0
        self._last_stream_notice: dict[str, float] = {}
        self._load()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "projects": [self._project_payload(project) for project in self.projects],
                "tasks": [self._task_payload(task) for task in self.tasks],
                "current_id": self.current_id,
                "approvals": [self._approval_payload(item) for item in self.approvals.values()],
                "settings": {
                    "model": self.settings.model,
                    "base_url": self.settings.base_url,
                    "context_tokens": self.settings.context_tokens,
                    "max_steps": self.settings.max_steps,
                    "approval_mode": self.settings.approval_mode,
                    "api_key_configured": bool(self.settings.api_key.strip()),
                    "remember_key": self.settings.remember_key,
                },
                "revision": self.revision,
            }

    def wait_for_state(
        self, after_revision: int, timeout: float = 15.0
    ) -> tuple[int, dict[str, Any]]:
        with self.events:
            if self.revision <= after_revision:
                self.events.wait_for(
                    lambda: self.revision > after_revision,
                    timeout=max(0.0, timeout),
                )
            return self.revision, self.snapshot()

    def add_project(self, raw_path: str) -> dict[str, Any]:
        path = self._workspace_path(raw_path)
        with self.lock:
            existing = next((project for project in self.projects if project.path == path), None)
            if existing is not None:
                return self._project_payload(existing)
            project = WebProject(uuid.uuid4().hex, path.name or str(path), path)
            self.projects.append(project)
            try:
                self._save()
            except Exception:
                self.projects.remove(project)
                raise
            return self._project_payload(project)

    def add_project_with_conversation(
        self, raw_path: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self._workspace_path(raw_path)
        with self.lock:
            project = next((item for item in self.projects if item.path == path), None)
            created_project = project is None
            if project is None:
                project = WebProject(uuid.uuid4().hex, path.name or str(path), path)
                self.projects.append(project)
            task = WebTask(
                id=uuid.uuid4().hex,
                project_id=project.id,
                permission_mode=normalize_permission_mode(self.settings.approval_mode),
                change_tracker=ConversationChangeTracker(project.path),
            )
            previous_current = self.current_id
            self.tasks.append(task)
            self.current_id = task.id
            try:
                self._save()
            except Exception:
                self.tasks.remove(task)
                self.current_id = previous_current
                if created_project:
                    self.projects.remove(project)
                raise
            return self._project_payload(project), self._task_payload(task)

    def new_conversation(self, project_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            project = self._project(project_id) if project_id else None
            task = WebTask(
                id=uuid.uuid4().hex,
                project_id=project.id if project else None,
                permission_mode=normalize_permission_mode(self.settings.approval_mode),
                change_tracker=ConversationChangeTracker(project.path if project else None),
            )
            self.tasks.append(task)
            self.current_id = task.id
            self._save()
            return self._task_payload(task)

    def rename_project(self, project_id: str, title: str) -> dict[str, Any]:
        name = self._display_name(title)
        with self.lock:
            project = self._project(project_id)
            project.title = name
            self._save()
            return self._project_payload(project)

    def rename_conversation(self, task_id: str, title: str) -> dict[str, Any]:
        name = self._display_name(title)
        with self.lock:
            task = self._task(task_id)
            task.title = name
            task.title_is_custom = True
            self._save()
            return self._task_payload(task)

    def remove_project(self, project_id: str) -> None:
        with self.lock:
            project = self._project(project_id)
            if any(task.running and task.project_id == project.id for task in self.tasks):
                raise RuntimeConflict("项目中仍有对话正在运行")
            if any(task.worktree_path is not None and task.project_id == project.id for task in self.tasks):
                raise RuntimeConflict("项目中仍有 worktree 隔离对话，请先保留分支并删除对应对话")
            self.projects.remove(project)
            for task in self.tasks:
                if task.project_id == project.id:
                    task.project_id = None
                    task.change_tracker.retarget(None)
            self._save()

    def remove_conversation(self, task_id: str) -> None:
        with self.lock:
            task = self._task(task_id)
            if task.running:
                raise RuntimeConflict("对话正在运行")
            if task.workspace_changing:
                raise RuntimeConflict("隔离工作区正在创建，暂时不能删除对话")
            self.tasks.remove(task)
            if self.current_id == task.id:
                self.current_id = self.tasks[0].id if self.tasks else None
            self._save()

    def bind_workspace(self, task_id: str, raw_path: str) -> dict[str, Any]:
        path = self._workspace_path(raw_path)
        with self.lock:
            task = self._task(task_id)
            if task.running or task.workspace_changing:
                raise RuntimeConflict("对话正在运行，不能更改工作目录")
            if task.worktree_path is not None:
                raise RuntimeConflict("当前对话已启用隔离 worktree，不能重新绑定工作目录")
            project = next((item for item in self.projects if item.path == path), None)
            created_project = project is None
            if project is None:
                project = WebProject(uuid.uuid4().hex, path.name or str(path), path)
                self.projects.append(project)
            previous_project_id = task.project_id
            previous_workspace = task.change_tracker.workspace
            previous_current = self.current_id
            task.project_id = project.id
            task.change_tracker.retarget(project.path)
            self.current_id = task.id
            try:
                self._save()
            except Exception:
                task.project_id = previous_project_id
                task.change_tracker.retarget(previous_workspace)
                self.current_id = previous_current
                if created_project:
                    self.projects.remove(project)
                raise
            return self._task_payload(task)

    def ensure_workspace_change_allowed(self, task_id: str) -> None:
        with self.lock:
            task = self._task(task_id)
            if task.running or task.workspace_changing:
                raise RuntimeConflict("对话正在运行，不能更改工作目录")
            if task.worktree_path is not None:
                raise RuntimeConflict("当前对话已启用隔离 worktree，不能重新绑定工作目录")

    def create_task_worktree(self, task_id: str) -> dict[str, Any]:
        from .worktree_service import WorktreeOperationError, WorktreeService

        with self.lock:
            task = self._task(task_id)
            project = self._project(task.project_id) if task.project_id else None
            if project is None:
                raise RuntimeConflict("请先为当前对话选择 Git 工作目录")
            if task.running or task.workspace_changing:
                raise RuntimeConflict("对话正在运行，不能创建隔离工作区")
            if task.worktree_path is not None:
                return self._task_payload(task)
            if task.change_tracker.changes:
                raise RuntimeConflict("当前对话已经产生文件改动，请新建对话后再启用 worktree 隔离")
            task.workspace_changing = True
            task.status = "正在创建隔离工作区"

        try:
            binding = WorktreeService(
                project.path,
                self.settings_root / ".coding-agent" / "worktrees",
            ).create(task.id)
            with self.lock:
                task = self._task(task_id)
                task.worktree_path = Path(binding["workspace"]).resolve()
                task.worktree_root = Path(binding["worktree_root"]).resolve()
                task.worktree_branch = str(binding["branch"])
                task.worktree_base_branch = (
                    str(binding["base_branch"]) if binding["base_branch"] else None
                )
                task.worktree_base_commit = str(binding["base_commit"])
                task.change_tracker.retarget(task.worktree_path)
                task.workspace_changing = False
                task.status = "隔离工作区已就绪"
                task.entries.append(
                    ChatEntry(
                        "system",
                        f"已为当前对话创建隔离分支 {task.worktree_branch}。"
                        "Agent、Diff 与 DevOps 操作只作用于该 worktree；主工作区不会自动合并。",
                    )
                )
                self._save()
                return self._task_payload(task)
        except Exception as exc:
            with self.lock:
                try:
                    task = self._task(task_id)
                except RuntimeNotFound:
                    raise
                task.workspace_changing = False
                task.status = "隔离工作区创建失败"
            if isinstance(exc, WorktreeOperationError):
                raise RuntimeConflict(str(exc)) from exc
            raise

    def select_conversation(self, task_id: str) -> None:
        with self.lock:
            self._task(task_id)
            self.current_id = task_id
            self._save()

    def set_permission_mode(self, task_id: str, mode: str) -> dict[str, Any]:
        normalized = normalize_permission_mode(mode, default="")
        if normalized not in PERMISSION_MODES:
            raise ValueError("权限模式只能是 request、risk 或 full")
        with self.lock:
            task = self._task(task_id)
            if task.running:
                raise RuntimeConflict("对话正在运行，不能更改权限模式")
            previous_task_mode = task.permission_mode
            previous_default = self.settings.approval_mode
            task.permission_mode = normalized
            self.settings.approval_mode = normalized
            try:
                self.settings.save(self.settings_root)
                self._save()
            except Exception:
                task.permission_mode = previous_task_mode
                self.settings.approval_mode = previous_default
                try:
                    self.settings.save(self.settings_root)
                except Exception:
                    pass
                raise
            return self._task_payload(task)

    def send_message(self, task_id: str, text: str) -> dict[str, Any]:
        message = text.strip()
        if not message:
            raise ValueError("任务不能为空")
        if len(message) > 100_000:
            raise ValueError("任务内容过长")
        with self.lock:
            task = self._task(task_id)
            if task.running:
                raise RuntimeConflict("当前对话正在运行")
            if task.workspace_changing:
                raise RuntimeConflict("隔离工作区正在创建，请稍后再发送任务")
            task.entries.append(ChatEntry("user", message))
            if not task.title_is_custom and task.title == "新对话":
                task.title = self._display_name(message)[:36]
            task.running = True
            task.status = "正在连接模型"
            task.streaming_content = ""
            task.progress = None
            task.cancel_event.clear()
            task.pending_change_paths.clear()
            task.change_tracker.begin_turn()
            self.current_id = task.id
            self._save()
            worker = threading.Thread(
                target=self._run_task,
                args=(task.id, message),
                name=f"coding-agent-{task.id[:8]}",
                daemon=True,
            )
            worker.start()
            return self._task_payload(task)

    def cancel(self, task_id: str) -> None:
        with self.lock:
            task = self._task(task_id)
            task.cancel_event.set()
            task.status = "正在停止"
            if task.progress is not None:
                task.progress = {
                    **task.progress,
                    "state": "cancelling",
                    "label": "正在终止 Docker 进程",
                }
            for approval in self.approvals.values():
                if approval.task_id == task.id:
                    approval.approved = False
                    approval.signal.set()
            self._touch()

    def resolve_approval(self, approval_id: str, approved: bool) -> None:
        with self.lock:
            approval = self.approvals.get(approval_id)
            if approval is None:
                raise RuntimeNotFound("审批请求不存在或已经处理")
            approval.approved = bool(approved)
            approval.signal.set()
            self._touch()

    def diff(self, task_id: str, path: str, entry_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            task = self._task(task_id)
            change = self._entry_change(task, entry_id, path)
            if change is None:
                raise RuntimeNotFound("文件改动不存在")
            rows: list[dict[str, Any]] = []
            for index, segment in enumerate(change.segments, start=1):
                if len(change.segments) > 1 or segment.drifted:
                    rows.append(
                        {
                            "kind": "segment",
                            "old_line": None,
                            "new_line": None,
                            "text": f"工作区：{segment.workspace} · 追踪段 {index}",
                        }
                    )
                old_text = segment.baseline.text
                new_text = segment.latest.text
                if old_text is None and not segment.baseline.exists:
                    old_text = ""
                if new_text is None and not segment.latest.exists:
                    new_text = ""
                if old_text is None or new_text is None:
                    rows.append(
                        {
                            "kind": "warning",
                            "old_line": None,
                            "new_line": None,
                            "text": segment.latest.reason
                            or segment.baseline.reason
                            or "没有可用的文本预览",
                        }
                    )
                    continue
                rows.extend(
                    {
                        "kind": row.kind,
                        "old_line": row.old_line,
                        "new_line": row.new_line,
                        "text": row.text,
                    }
                    for row in build_diff_rows(old_text, new_text)
                )
            task.review_path = path
            self._save()
            return {
                "path": change.path,
                "status": change.status,
                "added": change.added,
                "deleted": change.deleted,
                "warning": change.warning,
                "rows": rows,
            }

    @staticmethod
    def _entry_change(task: WebTask, entry_id: str | None, path: str) -> FileChange | None:
        if entry_id is None:
            return task.change_tracker.changes.get(path)
        entry = next((item for item in task.entries if item.id == entry_id), None)
        if entry is None:
            return None
        for raw_change in entry.file_changes:
            change = file_change_from_dict(raw_change)
            if change is not None and change.path == path:
                return change
        # Sessions saved before per-turn snapshots only have the cumulative diff.
        if path in entry.change_paths:
            return task.change_tracker.changes.get(path)
        return None

    def devops_overview(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self._task(task_id)
            project = next(
                (item for item in self.projects if item.id == task.project_id), None
            )
            if project is None:
                raise RuntimeConflict("当前对话尚未选择工作目录")
            workspace = self._task_workspace(task, project)

        from .devops_service import DevOpsOperationError, DevOpsService

        service = DevOpsService(
            workspace,
            release_state_root=self.settings_root / ".coding-agent" / "releases",
            release_identity_workspace=project.path,
        )
        inspected = service.inspect()
        environments: list[dict[str, Any]] = []
        for configured in inspected["environments"]:
            name = configured["name"]
            item: dict[str, Any] = {
                **configured,
                "active_version": None,
                "services": [],
                "releases": [],
                "rollback_events": [],
                "operation": {"environment": name, "busy": False, "owner": None},
                "error": None,
            }
            if not inspected["ready"]:
                item["error"] = {
                    "code": "compose_not_found",
                    "message": "项目中未找到 Docker Compose 配置",
                }
                environments.append(item)
                continue
            try:
                operation = service.operation_status(name)
                history = service.releases(name, limit=20)
                status = service.status(name)
                item.update(
                    {
                        "operation": operation,
                        "active_version": history["active_version"],
                        "services": [
                            self._service_status_summary(record)
                            for record in status["services"]
                        ],
                        "releases": [
                            self._release_summary(record)
                            for record in history["releases"]
                        ],
                        "rollback_events": [
                            self._rollback_event_summary(record)
                            for record in history["rollback_events"]
                        ],
                    }
                )
            except DevOpsOperationError as exc:
                item["error"] = {"code": exc.code, "message": str(exc)}
            environments.append(item)
        return {
            "workspace": str(workspace),
            "compose_file": inspected["compose_file"],
            "default_environment": inspected["default_environment"],
            "release_policy": inspected["release_policy"],
            "github_actions": inspected["github_actions"],
            "environments": environments,
        }

    @staticmethod
    def _service_status_summary(record: dict[str, Any]) -> dict[str, str]:
        return {
            "service": str(
                record.get("Service") or record.get("service") or record.get("Name") or "unknown"
            )[:120],
            "state": str(record.get("State") or record.get("state") or "unknown")[:40],
            "health": str(record.get("Health") or record.get("health") or "not-configured")[:40],
        }

    @staticmethod
    def _release_summary(record: dict[str, Any]) -> dict[str, Any]:
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        git = provenance.get("git") if isinstance(provenance.get("git"), dict) else {}
        actions = (
            provenance.get("github_actions")
            if isinstance(provenance.get("github_actions"), dict)
            else {}
        )
        return {
            "version": str(record.get("version") or ""),
            "status": str(record.get("status") or "unknown"),
            "created_at": str(record.get("created_at") or ""),
            "healthy": bool(record.get("healthy")),
            "services": [str(item) for item in record.get("services", []) if isinstance(item, str)],
            "images": [
                {
                    "reference": str(item.get("reference") or ""),
                    "id": str(item.get("id") or "")[:24],
                }
                for item in record.get("images", [])
                if isinstance(item, dict)
            ],
            "git": {
                "commit": git.get("commit"),
                "branch": git.get("branch"),
                "dirty": git.get("dirty"),
            },
            "github_actions": actions,
        }

    @staticmethod
    def _rollback_event_summary(record: dict[str, Any]) -> dict[str, str]:
        return {
            "from_version": str(record.get("from_version") or ""),
            "target_version": str(record.get("target_version") or ""),
            "status": str(record.get("status") or "unknown")[:40],
            "started_at": str(record.get("started_at") or ""),
            "finished_at": str(record.get("finished_at") or ""),
        }

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"api_key", "model", "base_url", "context_tokens", "max_steps", "approval_mode", "remember_key"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"未知设置: {', '.join(sorted(unknown))}")
        with self.lock:
            if "api_key" in values and str(values["api_key"]).strip():
                self.settings.api_key = str(values["api_key"]).strip()
            if "model" in values:
                self.settings.model = str(values["model"]).strip()
            if "base_url" in values:
                self.settings.base_url = str(values["base_url"]).strip()
            if "context_tokens" in values:
                self.settings.context_tokens = self._positive_int(values["context_tokens"], "上下文预算")
            if "max_steps" in values:
                self.settings.max_steps = self._positive_int(values["max_steps"], "最大步骤")
            if "approval_mode" in values:
                mode = normalize_permission_mode(values["approval_mode"], default="")
                if mode not in PERMISSION_MODES:
                    raise ValueError("权限模式只能是 request、risk 或 full")
                self.settings.approval_mode = mode
            if "remember_key" in values:
                self.settings.remember_key = bool(values["remember_key"])
            if not self.settings.model:
                raise ValueError("模型名称不能为空")
            self.settings.save(self.settings_root)
            self._touch()
            return self.snapshot()["settings"]

    def _run_task(self, task_id: str, message: str) -> None:
        agent: CodingAgent | None = None
        try:
            with self.lock:
                task = self._task(task_id)
                agent = self._make_agent(task)
                if task.history:
                    agent.restore_history(task.history)
            result = agent.run(message)
            with self.lock:
                task = self._task(task_id)
                task.history = list(agent.history)
                task.execution_state = agent.execution_state
                visible_paths = tuple(
                    path for path in task.pending_change_paths if path in task.change_tracker.turn_changes
                )
                task.entries.append(
                    ChatEntry(
                        "assistant",
                        result,
                        visible_paths,
                        file_changes=task.change_tracker.serialize_turn(),
                    )
                )
                task.streaming_content = ""
                task.pending_change_paths.clear()
                task.running = False
                task.status = (
                    "已完成"
                    if task.execution_state.outcome == "completed"
                    else "已完成 · 未验证"
                )
                self._save()
        except AgentCancelled:
            self._finish_error(task_id, "system", "任务已停止", agent)
        except (AgentStopped, ModelError, ValueError) as exc:
            self._finish_error(task_id, "error", str(exc), agent)
        except Exception as exc:
            self._finish_error(task_id, "error", f"任务执行失败: {type(exc).__name__}: {exc}", agent)
        finally:
            if agent is not None:
                agent.close()

    def _finish_error(
        self,
        task_id: str,
        kind: str,
        text: str,
        agent: CodingAgent | None,
    ) -> None:
        with self.lock:
            try:
                task = self._task(task_id)
            except RuntimeNotFound:
                return
            if agent is not None:
                task.history = list(agent.history)
                task.execution_state = agent.execution_state
                if kind == "system":
                    task.execution_state.mark_cancelled()
                else:
                    task.execution_state.mark_failed()
            visible_paths = tuple(
                path for path in task.pending_change_paths if path in task.change_tracker.turn_changes
            )
            task.entries.append(
                ChatEntry(
                    kind,
                    text,
                    visible_paths,
                    file_changes=task.change_tracker.serialize_turn(),
                )
            )
            task.streaming_content = ""
            task.pending_change_paths.clear()
            task.running = False
            task.status = "已停止" if kind == "system" else "发生错误"
            if (
                task.progress is not None
                and task.progress.get("state") != "completed"
                and (kind == "system" or task.progress.get("state") not in {"failed", "cancelled"})
            ):
                task.progress = {
                    **task.progress,
                    "state": "cancelled" if kind == "system" else "failed",
                    "label": "部署已停止" if kind == "system" else "部署执行失败",
                }
            self._save()

    def _make_agent(self, task: WebTask) -> CodingAgent:
        project = self._project(task.project_id) if task.project_id else None
        workspace = self._task_workspace(task, project)
        project_policy = load_project_policy(workspace) if workspace else None
        # Config validates model settings and requires a directory. The settings
        # root is used only for that validation in projectless chat; the tool
        # provider still receives None, so local tools cannot fall back here.
        config = Config.from_values(
            api_key=self.settings.api_key,
            model=self.settings.model,
            base_url=self.settings.base_url,
            workspace=workspace or self.settings_root,
            context_tokens=self.settings.context_tokens,
            max_steps=self.settings.max_steps,
            approval_mode=task.permission_mode,
        )
        model = self.model_factory() if self.model_factory else OpenAIChatModel(
            api_key=config.api_key,
            model=config.model,
            base_url=config.base_url,
            timeout=config.request_timeout,
            max_retries=config.max_retries,
        )
        model_call_lock = threading.RLock()
        tools = build_default_tool_provider(
            workspace,
            approver=lambda command, risk, reason: self._request_approval(
                task.id, command, risk, reason
            ),
            is_cancelled=task.cancel_event.is_set,
            on_progress=lambda data: self._handle_agent_event(
                task.id, "tool_progress", data
            ),
            devops_state_root=self.settings_root / ".coding-agent" / "releases",
            devops_release_identity_workspace=project.path if project else None,
            approval_mode=task.permission_mode,
            change_tracker=task.change_tracker,
            task_list_state=task.task_list,
            on_task_list_update=lambda snapshot: self._handle_task_list_update(
                task.id, snapshot
            ),
            extension_root=self.settings_root,
            sampling_model=model,
            sampling_model_lock=model_call_lock,
        )
        return CodingAgent(
            model,
            tools,
            ContextManager(config.context_tokens),
            max_steps=config.max_steps,
            on_event=lambda name, data: self._handle_agent_event(task.id, name, data),
            is_cancelled=task.cancel_event.is_set,
            system_prompt=(
                with_project_rules(SYSTEM_PROMPT, project_policy.rules)
                if project_policy
                else PROJECTLESS_SYSTEM_PROMPT
            ),
            task_list=task.task_list,
            model_call_lock=model_call_lock,
            execution_state=task.execution_state,
            validation_commands=(
                project_policy.validation_commands if project_policy else ()
            ),
        )

    def _handle_task_list_update(
        self, task_id: str, _snapshot: dict[str, Any]
    ) -> None:
        with self.lock:
            task = self._task(task_id)
            task.status = "任务清单已更新"
            self._save()

    def _handle_agent_event(self, task_id: str, name: str, data: dict[str, Any]) -> None:
        with self.lock:
            try:
                task = self._task(task_id)
            except RuntimeNotFound:
                return
            if name == "model_start":
                task.streaming_content = ""
                task.status = f"模型思考中 · {data.get('step', 1)}/{data.get('max_steps', 1)}"
            elif name == "assistant_delta":
                content = data.get("content")
                if isinstance(content, str):
                    task.streaming_content = content[:200_000]
                task.status = "模型正在回答"
                now = time.monotonic()
                if now - self._last_stream_notice.get(task.id, 0.0) >= 0.05:
                    self._last_stream_notice[task.id] = now
                    self._touch()
                return
            elif name == "decision_summary":
                content = data.get("content")
                if isinstance(content, str) and content.strip():
                    raw_tools = data.get("tools")
                    safe_tools = (
                        tuple(
                            tool[:100]
                            for tool in raw_tools[:16]
                            if isinstance(tool, str) and tool
                        )
                        if isinstance(raw_tools, list)
                        else ()
                    )
                    decision_number = 1 + sum(
                        entry.kind == "decision_summary" for entry in task.entries
                    )
                    task.streaming_content = ""
                    task.entries.append(
                        ChatEntry(
                            "decision_summary",
                            self._visible_decision_summary(content),
                            # A decision summary number belongs to the visible
                            # conversation timeline.  The model-loop step can
                            # jump after tool retries, context compaction, or a
                            # completion-gate continuation, so it must not be
                            # used as the user-facing sequence number.
                            step=decision_number,
                            tools=safe_tools,
                        )
                    )
                    task.status = "决策摘要已生成，准备执行工具"
            elif name == "summary_start":
                task.status = "正在压缩上下文"
            elif name == "checkpoint":
                history = data.get("history")
                if isinstance(history, list) and all(
                    isinstance(message, dict) for message in history
                ):
                    task.history = [dict(message) for message in history]
                self._save()
                return
            elif name == "tool_start":
                task.streaming_content = ""
                task.status = f"正在执行 {data.get('name', '工具')}"
                tool_name = str(data.get("name") or "")
                if tool_name.startswith("compose_"):
                    task.progress = {
                        "operation": tool_name,
                        "environment": self._tool_environment(data.get("arguments")),
                        "phase": "queued",
                        "label": "准备执行",
                        "current": 0,
                        "total": 1,
                        "percent": 0,
                        "elapsed_seconds": 0.0,
                        "state": "running",
                    }
            elif name == "tool_progress":
                task.progress = self._progress_payload(data)
                task.status = str(task.progress["label"])
            elif name == "tool_end":
                ok = bool(data.get("ok"))
                detail = str(data.get("output") or data.get("error") or "")
                task.entries.append(
                    ChatEntry("tool" if ok else "error", f"{data.get('name', '工具')}\n{detail}".strip())
                )
                changes = data.get("changes")
                if isinstance(changes, dict):
                    for path in changes.get("paths", []):
                        if not isinstance(path, str):
                            continue
                        if path in task.change_tracker.turn_changes:
                            if path not in task.pending_change_paths:
                                task.pending_change_paths.append(path)
                        elif path in task.pending_change_paths:
                            task.pending_change_paths.remove(path)
                task.status = "工具完成，继续分析" if ok else "工具失败，正在恢复"
                tool_name = str(data.get("name") or "")
                if tool_name.startswith("compose_") and task.progress is not None:
                    cancelled = task.cancel_event.is_set()
                    task.progress = {
                        **task.progress,
                        "state": "cancelled" if cancelled else ("completed" if ok else "failed"),
                        "percent": 100 if ok else task.progress.get("percent", 0),
                        "label": "部署已停止" if cancelled else ("部署步骤完成" if ok else "部署步骤失败"),
                    }
            if name == "tool_progress":
                self._touch()
            else:
                self._save()

    def _request_approval(
        self,
        task_id: str,
        command: str,
        risk: RiskLevel,
        reason: str,
    ) -> bool:
        approval = PendingApproval(uuid.uuid4().hex, task_id, command, risk.value, reason)
        with self.lock:
            self.approvals[approval.id] = approval
            task = self._task(task_id)
            task.status = "等待操作审批"
            self._touch()
        try:
            while not approval.signal.wait(0.1):
                with self.lock:
                    task = self._task(task_id)
                    if task.cancel_event.is_set():
                        return False
            return approval.approved
        finally:
            with self.lock:
                self.approvals.pop(approval.id, None)
                self._touch()

    def _load(self) -> None:
        state = self.store.load()
        for raw in state.get("projects", []):
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                continue
            path = Path(raw["path"]).expanduser().resolve()
            if not path.is_dir():
                continue
            self.projects.append(
                WebProject(str(raw.get("id") or uuid.uuid4().hex), str(raw.get("title") or path.name), path)
            )
        projects = {project.id: project for project in self.projects}
        for raw in state.get("tasks", []):
            if not isinstance(raw, dict):
                continue
            project_id = raw.get("project_id") if isinstance(raw.get("project_id"), str) else None
            project = projects.get(project_id)
            stored_worktree = raw.get("worktree") if isinstance(raw.get("worktree"), dict) else {}
            worktree_path = self._stored_directory(stored_worktree.get("workspace"))
            worktree_root = self._stored_directory(stored_worktree.get("root"))
            if worktree_path is None or worktree_root is None:
                worktree_path = None
                worktree_root = None
            tracker = ConversationChangeTracker(
                worktree_path or (project.path if project else None)
            )
            tracker.load_serialized(raw.get("file_changes", []))
            entries = self._entries_from_storage(raw.get("entries"))
            pending = self._collapse_legacy_changes(entries)
            remaining_paths = set(tracker.changes)
            for entry in entries:
                entry.change_paths = tuple(
                    path for path in entry.change_paths if path in remaining_paths
                )
            pending = [path for path in pending if path in remaining_paths]
            raw_pending = raw.get("pending_change_paths")
            if isinstance(raw_pending, list):
                for path in raw_pending:
                    if isinstance(path, str) and path not in pending:
                        pending.append(path)
            recovered = tuple(path for path in pending if path in tracker.changes)
            if recovered:
                entries.append(ChatEntry("system", "上次任务在结束前中断，以下为已经记录的文件改动。", recovered))
            history = raw.get("history")
            task = WebTask(
                id=str(raw.get("id") or uuid.uuid4().hex),
                project_id=project.id if project else None,
                permission_mode=normalize_permission_mode(
                    raw.get("permission_mode", self.settings.approval_mode)
                ),
                title=str(raw.get("title") or "新对话"),
                title_is_custom=bool(raw.get("title_is_custom", False)),
                entries=entries,
                history=list(history) if isinstance(history, list) else [],
                change_tracker=tracker,
                review_path=raw.get("review_path") if isinstance(raw.get("review_path"), str) else None,
                worktree_path=worktree_path,
                worktree_root=worktree_root,
                worktree_branch=(
                    str(stored_worktree["branch"])
                    if isinstance(stored_worktree.get("branch"), str)
                    else None
                ),
                worktree_base_branch=(
                    str(stored_worktree["base_branch"])
                    if isinstance(stored_worktree.get("base_branch"), str)
                    else None
                ),
                worktree_base_commit=(
                    str(stored_worktree["base_commit"])
                    if isinstance(stored_worktree.get("base_commit"), str)
                    else None
                ),
                task_list=TaskListState.from_storage(raw.get("task_list")),
                execution_state=ExecutionState.from_storage(raw.get("execution_state")),
            )
            if stored_worktree and worktree_path is None:
                task.entries.append(
                    ChatEntry("system", "保存的隔离 worktree 已不存在，当前对话已回退到项目工作区。")
                )
            self.tasks.append(task)
        current_id = state.get("current_id")
        if isinstance(current_id, str) and any(task.id == current_id for task in self.tasks):
            self.current_id = current_id

    def _save(self) -> None:
        self.store.save(
            {
                "version": 6,
                "current_id": self.current_id,
                "projects": [self._project_payload(project) for project in self.projects],
                "tasks": [
                    {
                        "id": task.id,
                        "project_id": task.project_id,
                        "permission_mode": task.permission_mode,
                        "title": task.title,
                        "title_is_custom": task.title_is_custom,
                        "entries": [
                            {
                                "id": entry.id,
                                "kind": entry.kind,
                                "text": entry.text,
                                "change_paths": list(entry.change_paths),
                                "file_changes": entry.file_changes,
                                "step": entry.step,
                                "tools": list(entry.tools),
                            }
                            for entry in task.entries
                        ],
                        "history": task.history,
                        "file_changes": task.change_tracker.serialize(),
                        "review_path": task.review_path,
                        "pending_change_paths": list(task.pending_change_paths),
                        "worktree": self._worktree_payload(task),
                        "task_list": self._task_list_storage(task),
                        "execution_state": task.execution_state.to_storage(),
                    }
                    for task in self.tasks
                ],
            }
        )
        self._touch()

    def _touch(self) -> None:
        with self.events:
            self.revision += 1
            self.events.notify_all()

    def _project(self, project_id: str | None) -> WebProject:
        project = next((item for item in self.projects if item.id == project_id), None)
        if project is None:
            raise RuntimeNotFound("项目不存在")
        return project

    def _task(self, task_id: str) -> WebTask:
        task = next((item for item in self.tasks if item.id == task_id), None)
        if task is None:
            raise RuntimeNotFound("对话不存在")
        return task

    @staticmethod
    def _workspace_path(raw_path: str) -> Path:
        text = raw_path.strip()
        if not text:
            raise ValueError("工作目录不能为空")
        requested = Path(text).expanduser()
        if not requested.is_absolute():
            raise ValueError("请输入绝对工作目录")
        path = requested.resolve()
        if not path.is_dir():
            raise ValueError(f"工作目录不存在或不是目录: {path}")
        return path

    @staticmethod
    def _project_payload(project: WebProject) -> dict[str, Any]:
        return {"id": project.id, "title": project.title, "path": str(project.path)}

    def _task_payload(self, task: WebTask) -> dict[str, Any]:
        project = next((item for item in self.projects if item.id == task.project_id), None)
        workspace = self._task_workspace(task, project)
        return {
            "id": task.id,
            "project_id": task.project_id,
            "permission_mode": task.permission_mode,
            "title": task.title,
            "running": task.running,
            "status": task.status,
            "streaming_content": task.streaming_content,
            "progress": task.progress,
            "workspace": str(workspace) if workspace else None,
            "source_workspace": str(project.path) if project else None,
            "worktree": self._worktree_payload(task),
            "workspace_changing": task.workspace_changing,
            "task_list": task.task_list.snapshot(),
            "execution": task.execution_state.completion_evidence(),
            "entries": [
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "text": entry.text,
                    "change_paths": list(entry.change_paths),
                    "change_scope": "turn" if entry.file_changes else "conversation",
                    "step": entry.step,
                    "tools": list(entry.tools),
                }
                for entry in task.entries
            ],
            "review_path": task.review_path,
        }

    @staticmethod
    def _stored_directory(value: Any) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        path = Path(value).expanduser().resolve()
        return path if path.is_dir() else None

    @staticmethod
    def _task_workspace(task: WebTask, project: WebProject | None) -> Path | None:
        if task.worktree_path is not None and task.worktree_path.is_dir():
            return task.worktree_path
        return project.path if project else None

    @staticmethod
    def _worktree_payload(task: WebTask) -> dict[str, Any] | None:
        if task.worktree_path is None or task.worktree_root is None:
            return None
        return {
            "workspace": str(task.worktree_path),
            "root": str(task.worktree_root),
            "branch": task.worktree_branch,
            "base_branch": task.worktree_base_branch,
            "base_commit": task.worktree_base_commit,
        }

    @staticmethod
    def _task_list_storage(task: WebTask) -> dict[str, Any]:
        snapshot = task.task_list.snapshot()
        return {
            "objective": snapshot["objective"],
            "items": snapshot["items"],
        }

    @staticmethod
    def _tool_environment(arguments: Any) -> str:
        if not isinstance(arguments, str):
            return "默认环境"
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return "默认环境"
        value = parsed.get("environment") if isinstance(parsed, dict) else None
        return value if isinstance(value, str) and value else "默认环境"

    @staticmethod
    def _progress_payload(data: dict[str, Any]) -> dict[str, Any]:
        total = max(1, min(int(data.get("total", 1)), 20))
        current = max(1, min(int(data.get("current", 1)), total))
        percent = max(0, min(int(data.get("percent", 0)), 100))
        elapsed = max(0.0, min(float(data.get("elapsed_seconds", 0.0)), 86_400.0))
        allowed_states = {"running", "completed", "failed", "cancelled", "cancelling"}
        state = str(data.get("state") or "running")
        return {
            "operation": str(data.get("operation") or "compose_operation")[:80],
            "environment": str(data.get("environment") or "默认环境")[:80],
            "phase": str(data.get("phase") or "running")[:80],
            "label": str(data.get("label") or "执行部署操作")[:120],
            "current": current,
            "total": total,
            "percent": percent,
            "elapsed_seconds": elapsed,
            "state": state if state in allowed_states else "running",
        }

    @staticmethod
    def _approval_payload(approval: PendingApproval) -> dict[str, Any]:
        return {
            "id": approval.id,
            "task_id": approval.task_id,
            "command": approval.command,
            "risk": approval.risk,
            "reason": approval.reason,
        }

    @staticmethod
    def _display_name(value: str) -> str:
        name = " ".join(value.replace("\n", " ").split())[:80]
        if not name:
            raise ValueError("名称不能为空")
        return name

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if parsed <= 0:
            raise ValueError(f"{label}必须大于 0")
        return parsed

    @staticmethod
    def _entries_from_storage(value: Any) -> list[ChatEntry]:
        if not isinstance(value, list):
            return []
        entries: list[ChatEntry] = []
        decision_number = 0
        for raw in value:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", "system"))
            if kind == "decision_summary":
                decision_number += 1
            paths = raw.get("change_paths")
            stored_changes: list[dict[str, Any]] = []
            raw_changes = raw.get("file_changes")
            if isinstance(raw_changes, list):
                for raw_change in raw_changes:
                    parsed = file_change_from_dict(raw_change)
                    if parsed is not None:
                        stored_changes.append(file_change_to_dict(parsed))
            entries.append(
                ChatEntry(
                    kind,
                    str(raw.get("text", "")),
                    tuple(path for path in paths if isinstance(path, str)) if isinstance(paths, list) else (),
                    id=str(raw.get("id") or uuid.uuid4().hex),
                    file_changes=stored_changes,
                    # Normalize legacy sessions whose stored value was the
                    # internal model-loop step rather than the visible order.
                    step=decision_number if kind == "decision_summary" else None,
                    tools=(
                        tuple(
                            tool
                            for tool in raw.get("tools", [])[:16]
                            if isinstance(tool, str) and tool
                        )
                        if isinstance(raw.get("tools"), list)
                        else ()
                    ),
                )
            )
        return entries

    def _visible_decision_summary(self, value: str) -> str:
        redacted = value.strip()
        secret = self.settings.api_key.strip()
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = _AUTHORIZATION_VALUE.sub(
            lambda match: f"authorization{match.group(1)}[REDACTED]",
            redacted,
        )
        redacted = _BEARER_TOKEN.sub("Bearer [REDACTED]", redacted)
        redacted = _SENSITIVE_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            redacted,
        )
        return redacted[:MAX_DECISION_SUMMARY_CHARS]

    @staticmethod
    def _collapse_legacy_changes(entries: list[ChatEntry]) -> list[str]:
        unfinished: list[str] = []
        current: list[ChatEntry] = []

        def collapse(group: list[ChatEntry]) -> None:
            paths: list[str] = []
            for entry in group:
                for path in entry.change_paths:
                    if path not in paths:
                        paths.append(path)
                entry.change_paths = ()
            target = next((entry for entry in reversed(group) if entry.kind in {"assistant", "error", "system"}), None)
            if target is None:
                unfinished.extend(path for path in paths if path not in unfinished)
            elif paths:
                target.change_paths = tuple(paths)

        for entry in entries:
            if entry.kind == "user" and current:
                collapse(current)
                current = []
            current.append(entry)
        collapse(current)
        return unfinished
