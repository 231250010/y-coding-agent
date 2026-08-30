from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent import AgentCancelled, AgentStopped, CodingAgent
from .changes import ConversationChangeTracker, build_diff_rows
from .config import Config
from .context import ContextManager
from .local_settings import LocalSettings
from .model import ChatModel, ModelError, OpenAIChatModel
from .permissions import PERMISSION_MODES, normalize_permission_mode
from .prompts import PROJECTLESS_SYSTEM_PROMPT, SYSTEM_PROMPT
from .safety import RiskLevel
from .session_store import SessionStore
from .providers import build_default_tool_provider


class RuntimeNotFound(LookupError):
    """Raised when a requested project, conversation, approval, or change does not exist."""


class RuntimeConflict(RuntimeError):
    """Raised when an operation conflicts with a running conversation."""


@dataclass(slots=True)
class ChatEntry:
    kind: str
    text: str
    change_paths: tuple[str, ...] = ()


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
    progress: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    change_tracker: ConversationChangeTracker = field(
        default_factory=lambda: ConversationChangeTracker(None)
    )
    pending_change_paths: list[str] = field(default_factory=list)
    review_path: str | None = None


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
            }

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
            self.tasks.remove(task)
            if self.current_id == task.id:
                self.current_id = self.tasks[0].id if self.tasks else None
            self._save()

    def bind_workspace(self, task_id: str, raw_path: str) -> dict[str, Any]:
        path = self._workspace_path(raw_path)
        with self.lock:
            task = self._task(task_id)
            if task.running:
                raise RuntimeConflict("对话正在运行，不能更改工作目录")
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
            if task.running:
                raise RuntimeConflict("对话正在运行，不能更改工作目录")

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
            task.entries.append(ChatEntry("user", message))
            if not task.title_is_custom and task.title == "新对话":
                task.title = self._display_name(message)[:36]
            task.running = True
            task.status = "正在连接模型"
            task.progress = None
            task.cancel_event.clear()
            task.pending_change_paths.clear()
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

    def resolve_approval(self, approval_id: str, approved: bool) -> None:
        with self.lock:
            approval = self.approvals.get(approval_id)
            if approval is None:
                raise RuntimeNotFound("审批请求不存在或已经处理")
            approval.approved = bool(approved)
            approval.signal.set()

    def diff(self, task_id: str, path: str) -> dict[str, Any]:
        with self.lock:
            task = self._task(task_id)
            change = task.change_tracker.changes.get(path)
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
            return self.snapshot()["settings"]

    def _run_task(self, task_id: str, message: str) -> None:
        agent: CodingAgent | None = None
        try:
            with self.lock:
                task = self._task(task_id)
                agent = self._make_agent(task)
                if task.history:
                    agent.history = [agent.history[0], *task.history[1:]]
            result = agent.run(message)
            with self.lock:
                task = self._task(task_id)
                task.history = list(agent.history)
                visible_paths = tuple(
                    path for path in task.pending_change_paths if path in task.change_tracker.changes
                )
                task.entries.append(ChatEntry("assistant", result, visible_paths))
                task.pending_change_paths.clear()
                task.running = False
                task.status = "已完成"
                self._save()
        except AgentCancelled:
            self._finish_error(task_id, "system", "任务已停止", agent)
        except (AgentStopped, ModelError, ValueError) as exc:
            self._finish_error(task_id, "error", str(exc), agent)
        except Exception as exc:
            self._finish_error(task_id, "error", f"任务执行失败: {type(exc).__name__}: {exc}", agent)

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
            visible_paths = tuple(
                path for path in task.pending_change_paths if path in task.change_tracker.changes
            )
            task.entries.append(ChatEntry(kind, text, visible_paths))
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
        workspace = project.path if project else None
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
            approval_mode=task.permission_mode,
            change_tracker=task.change_tracker,
        )
        return CodingAgent(
            model,
            tools,
            ContextManager(config.context_tokens),
            max_steps=config.max_steps,
            on_event=lambda name, data: self._handle_agent_event(task.id, name, data),
            is_cancelled=task.cancel_event.is_set,
            system_prompt=SYSTEM_PROMPT if workspace else PROJECTLESS_SYSTEM_PROMPT,
        )

    def _handle_agent_event(self, task_id: str, name: str, data: dict[str, Any]) -> None:
        with self.lock:
            try:
                task = self._task(task_id)
            except RuntimeNotFound:
                return
            if name == "model_start":
                task.status = f"模型思考中 · {data.get('step', 1)}/{data.get('max_steps', 1)}"
            elif name == "summary_start":
                task.status = "正在压缩上下文"
            elif name == "tool_start":
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
                        if path in task.change_tracker.changes:
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
            if name != "tool_progress":
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
            tracker = ConversationChangeTracker(project.path if project else None)
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
            )
            self.tasks.append(task)
        current_id = state.get("current_id")
        if isinstance(current_id, str) and any(task.id == current_id for task in self.tasks):
            self.current_id = current_id

    def _save(self) -> None:
        self.store.save(
            {
                "version": 3,
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
                            {"kind": entry.kind, "text": entry.text, "change_paths": list(entry.change_paths)}
                            for entry in task.entries
                        ],
                        "history": task.history,
                        "file_changes": task.change_tracker.serialize(),
                        "review_path": task.review_path,
                        "pending_change_paths": list(task.pending_change_paths),
                    }
                    for task in self.tasks
                ],
            }
        )

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
        return {
            "id": task.id,
            "project_id": task.project_id,
            "permission_mode": task.permission_mode,
            "title": task.title,
            "running": task.running,
            "status": task.status,
            "progress": task.progress,
            "workspace": str(project.path) if project else None,
            "entries": [
                {"kind": entry.kind, "text": entry.text, "change_paths": list(entry.change_paths)}
                for entry in task.entries
            ],
            "review_path": task.review_path,
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
        for raw in value:
            if not isinstance(raw, dict):
                continue
            paths = raw.get("change_paths")
            entries.append(
                ChatEntry(
                    str(raw.get("kind", "system")),
                    str(raw.get("text", "")),
                    tuple(path for path in paths if isinstance(path, str)) if isinstance(paths, list) else (),
                )
            )
        return entries

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
