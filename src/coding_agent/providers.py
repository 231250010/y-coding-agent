from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Protocol, TYPE_CHECKING

from .changes import ConversationChangeTracker

if TYPE_CHECKING:
    from .tools import ToolResult


class ToolProvider(Protocol):
    """Minimal boundary implemented by every local tool collection."""

    def schemas(self) -> list[dict[str, Any]]: ...

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class CompositeToolProvider:
    """Expose multiple providers as one deterministic tool namespace."""

    def __init__(self, providers: Sequence[ToolProvider]) -> None:
        self._schemas: list[dict[str, Any]] = []
        self._owners: dict[str, ToolProvider] = {}
        self.change_tracker: ConversationChangeTracker | None = None
        for provider in providers:
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


def build_default_tool_provider(
    workspace: Path | None,
    *,
    approver: Callable[..., bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    devops_state_root: Path | None = None,
    approval_mode: str = "risk",
    change_tracker: ConversationChangeTracker | None = None,
) -> ToolProvider:
    """Compose file/command, Git, and Docker Compose DevOps tools."""
    from .git_service import GitService
    from .git_tools import GitToolProvider
    from .devops_service import DevOpsService
    from .devops_tools import DevOpsToolProvider
    from .tools import ToolRegistry

    local = ToolRegistry(
        workspace,
        approver=approver,
        is_cancelled=is_cancelled,
        approval_mode=approval_mode,
        change_tracker=change_tracker,
    )
    if workspace is None:
        return local
    git = GitToolProvider(
        GitService(workspace),
        approver=approver,
        approval_mode=approval_mode,
        change_tracker=change_tracker,
    )
    devops = DevOpsToolProvider(
        DevOpsService(
            workspace,
            is_cancelled=is_cancelled,
            on_progress=on_progress,
            release_state_root=devops_state_root,
        ),
        approver=approver,
        approval_mode=approval_mode,
        change_tracker=change_tracker,
    )
    return CompositeToolProvider([local, git, devops])
