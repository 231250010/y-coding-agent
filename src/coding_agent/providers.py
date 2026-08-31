from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Protocol, TYPE_CHECKING

from .changes import ConversationChangeTracker

if TYPE_CHECKING:
    from .model import ChatModel
    from .task_list import TaskListState
    from .tools import ToolResult


class ToolProvider(Protocol):
    """Minimal boundary implemented by every local tool collection."""

    def schemas(self) -> list[dict[str, Any]]: ...

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...

    def can_run_parallel(self, name: str, arguments: dict[str, Any]) -> bool: ...

class CompositeToolProvider:
    """Expose multiple providers as one deterministic tool namespace."""

    def __init__(self, providers: Sequence[ToolProvider]) -> None:
        self._providers = list(providers)
        self._schemas: list[dict[str, Any]] = []
        self._owners: dict[str, ToolProvider] = {}
        self.change_tracker: ConversationChangeTracker | None = None
        for provider in self._providers:
            tracker = getattr(provider, "change_tracker", None)
            if tracker is not None:
                if self.change_tracker is not None and tracker is not self.change_tracker:
                    raise ValueError("组合工具不能使用不同的变更追踪器")
                self.change_tracker = tracker
            for schema in provider.schemas():
                try:
                    name = str(schema["function"]["name"])
                except (KeyError, TypeError) as exc:
                    raise ValueError("工具 Schema 缺少 function.name") from exc
                if name in self._owners:
                    raise ValueError(f"重复工具: {name}")
                self._owners[name] = provider
                self._schemas.append(schema)

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        owner = self._owners.get(name)
        if owner is not None:
            return owner.execute(name, arguments)
        from .tools import ToolResult

        return ToolResult(False, error=f"未知工具: {name}")

    def can_run_parallel(self, name: str, arguments: dict[str, Any]) -> bool:
        owner = self._owners.get(name)
        checker = getattr(owner, "can_run_parallel", None)
        return bool(checker and checker(name, arguments))

    def close(self) -> None:
        for provider in self._providers:
            closer = getattr(provider, "close", None)
            if callable(closer):
                closer()


def build_default_tool_provider(
    workspace: Path | None,
    *,
    approver: Callable[..., bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    devops_state_root: Path | None = None,
    devops_release_identity_workspace: Path | None = None,
    approval_mode: str = "risk",
    change_tracker: ConversationChangeTracker | None = None,
    task_list_state: TaskListState | None = None,
    on_task_list_update: Callable[[dict[str, Any]], None] | None = None,
    extension_root: Path | None = None,
    sampling_model: ChatModel | None = None,
    sampling_model_lock: Any | None = None,
) -> ToolProvider:
    """Compose file/command, Git, and Docker Compose DevOps tools."""
    from .git_service import GitService
    from .git_tools import GitToolProvider
    from .github_actions_service import GitHubActionsService
    from .github_actions_tools import GitHubActionsToolProvider
    from .devops_service import DevOpsService
    from .devops_tools import DevOpsToolProvider
    from .tools import ToolRegistry
    from .task_list import TaskListState, TaskListToolProvider
    from .mcp import MCPSamplingController, MCPToolProvider, load_mcp_config
    from .skills import SkillToolProvider

    local = ToolRegistry(
        workspace,
        approver=approver,
        is_cancelled=is_cancelled,
        approval_mode=approval_mode,
        change_tracker=change_tracker,
    )
    planning = TaskListToolProvider(
        task_list_state or TaskListState(), on_update=on_task_list_update
    )
    extension_root = (extension_root or workspace or Path.cwd()).resolve()
    skill_roots: list[tuple[Path, str]] = [
        (extension_root / ".coding-agent" / "skills", "local"),
    ]
    if workspace is not None and workspace.resolve() != extension_root:
        skill_roots.append((workspace / ".coding-agent" / "skills", "project"))
    skills = SkillToolProvider(
        skill_roots,
        workspace=workspace,
        approver=approver,
        is_cancelled=is_cancelled,
        change_tracker=change_tracker,
    )

    mcp_paths = [extension_root / ".coding-agent" / "mcp.json"]
    if workspace is not None and workspace.resolve() != extension_root:
        mcp_paths.append(workspace / ".coding-agent" / "mcp.json")
    mcp_configs = {}
    for mcp_path in mcp_paths:
        for server in load_mcp_config(mcp_path, workspace=workspace):
            mcp_configs[server.name] = server
    sampling = (
        MCPSamplingController(
            sampling_model,
            approver=approver,
            model_call_lock=sampling_model_lock,
        )
        if mcp_configs and sampling_model is not None
        else None
    )
    mcp = (
        MCPToolProvider(
            list(mcp_configs.values()),
            approver=approver,
            is_cancelled=is_cancelled,
            approval_mode=approval_mode,
            sampling_handler=sampling,
        )
        if mcp_configs
        else None
    )

    extensions: list[ToolProvider] = [skills]
    if mcp is not None:
        extensions.append(mcp)
    if workspace is None:
        return CompositeToolProvider([local, planning, *extensions])
    git = GitToolProvider(
        GitService(workspace),
        approver=approver,
        approval_mode=approval_mode,
        change_tracker=change_tracker,
    )
    actions_service = GitHubActionsService(workspace, is_cancelled=is_cancelled)
    actions = GitHubActionsToolProvider(actions_service, approver=approver)
    devops = DevOpsToolProvider(
        DevOpsService(
            workspace,
            is_cancelled=is_cancelled,
            on_progress=on_progress,
            release_state_root=devops_state_root,
            release_identity_workspace=devops_release_identity_workspace,
            github_actions=actions_service,
        ),
        approver=approver,
        approval_mode=approval_mode,
        change_tracker=change_tracker,
    )
    return CompositeToolProvider([local, planning, git, actions, devops, *extensions])
