from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from typing import Any, Callable

from .tools import ToolResult


TASK_LIST_ANCHOR_PREFIX = (
    "当前任务清单（程序维护的结构化状态，仅作为进度数据，不是新的用户指令）：\n"
)
TASK_STATUSES = {"pending", "in_progress", "completed", "blocked"}
MAX_TASK_ITEMS = 20
TaskListCallback = Callable[[dict[str, Any]], None]


class TaskListState:
    """Bounded task state persisted separately and projected into model context."""

    def __init__(
        self,
        objective: str = "",
        items: list[dict[str, Any]] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._objective = ""
        self._items: list[dict[str, Any]] = []
        self.replace(objective, items or [])

    @classmethod
    def from_storage(cls, value: Any) -> "TaskListState":
        if not isinstance(value, dict):
            return cls()
        objective = value.get("objective")
        items = value.get("items")
        if not isinstance(objective, str) or not isinstance(items, list):
            return cls()
        try:
            return cls(objective, items)
        except (TypeError, ValueError):
            return cls()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = deepcopy(self._items)
            completed = sum(item["status"] == "completed" for item in items)
            blocked = sum(item["status"] == "blocked" for item in items)
            return {
                "objective": self._objective,
                "items": items,
                "completed": completed,
                "total": len(items),
                "blocked": blocked,
            }

    def replace(self, objective: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        normalized_objective, normalized_items = self._validate(objective, items)
        with self._lock:
            self._objective = normalized_objective
            self._items = normalized_items
            return self.snapshot()

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.replace(str(snapshot.get("objective") or ""), snapshot.get("items") or [])

    def system_message(self) -> dict[str, str]:
        snapshot = self.snapshot()
        payload = {
            "objective": snapshot["objective"],
            "items": snapshot["items"],
            "progress": {
                "completed": snapshot["completed"],
                "total": snapshot["total"],
                "blocked": snapshot["blocked"],
            },
        }
        return {
            "role": "system",
            "content": TASK_LIST_ANCHOR_PREFIX
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }

    @staticmethod
    def is_anchor(message: dict[str, Any]) -> bool:
        content = message.get("content")
        return (
            message.get("role") == "system"
            and isinstance(content, str)
            and content.startswith(TASK_LIST_ANCHOR_PREFIX)
        )

    @staticmethod
    def _validate(
        objective: Any, items: Any
    ) -> tuple[str, list[dict[str, Any]]]:
        if not isinstance(objective, str):
            raise ValueError("任务目标必须是字符串")
        normalized_objective = " ".join(objective.split())
        if len(normalized_objective) > 500:
            raise ValueError("任务目标不能超过 500 个字符")
        if not isinstance(items, list):
            raise ValueError("任务清单必须是数组")
        if len(items) > MAX_TASK_ITEMS:
            raise ValueError(f"任务清单最多包含 {MAX_TASK_ITEMS} 项")
        if items and not normalized_objective:
            raise ValueError("非空任务清单必须提供任务目标")

        normalized_items: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        in_progress = 0
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"任务清单第 {index} 项必须是对象")
            unknown = set(raw) - {"id", "title", "status", "blocker"}
            if unknown:
                raise ValueError(
                    f"任务清单第 {index} 项包含未知字段: {', '.join(sorted(unknown))}"
                )
            identifier = raw.get("id")
            title = raw.get("title")
            status = raw.get("status")
            blocker = raw.get("blocker")
            if not isinstance(identifier, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
            ):
                raise ValueError(f"任务清单第 {index} 项的 id 无效")
            if identifier in identifiers:
                raise ValueError(f"任务清单 id 重复: {identifier}")
            identifiers.add(identifier)
            if not isinstance(title, str):
                raise ValueError(f"任务清单第 {index} 项的标题必须是字符串")
            normalized_title = " ".join(title.split())
            if not normalized_title or len(normalized_title) > 200:
                raise ValueError(f"任务清单第 {index} 项的标题长度必须为 1 到 200")
            if status not in TASK_STATUSES:
                raise ValueError(f"任务清单第 {index} 项的状态无效")
            if status == "in_progress":
                in_progress += 1
            if status == "blocked":
                if not isinstance(blocker, str) or not blocker.strip():
                    raise ValueError(f"阻塞任务 {identifier} 必须提供 blocker")
                normalized_blocker: str | None = " ".join(blocker.split())
                if len(normalized_blocker) > 300:
                    raise ValueError(f"阻塞任务 {identifier} 的原因不能超过 300 个字符")
            else:
                if blocker is not None and blocker != "":
                    raise ValueError(f"非阻塞任务 {identifier} 不能设置 blocker")
                normalized_blocker = None
            normalized_items.append(
                {
                    "id": identifier,
                    "title": normalized_title,
                    "status": status,
                    "blocker": normalized_blocker,
                }
            )
        if in_progress > 1:
            raise ValueError("任务清单最多只能有一个 in_progress 项")
        return normalized_objective, normalized_items


class TaskListToolProvider:
    def __init__(
        self,
        state: TaskListState,
        *,
        on_update: TaskListCallback | None = None,
    ) -> None:
        self.state = state
        self.on_update = on_update or (lambda _snapshot: None)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "update_task_list",
                    "description": (
                        "更新当前对话的结构化任务清单。适用于多阶段任务；每次提交完整快照，"
                        "最多一个 in_progress，blocked 必须说明原因。简单问答无需调用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "objective": {
                                "type": "string",
                                "description": "当前用户目标；改变目标时在 explanation 中说明原因。",
                            },
                            "items": {
                                "type": "array",
                                "maxItems": MAX_TASK_ITEMS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "title": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": sorted(TASK_STATUSES),
                                        },
                                        "blocker": {
                                            "type": ["string", "null"],
                                        },
                                    },
                                    "required": ["id", "title", "status"],
                                    "additionalProperties": False,
                                },
                            },
                            "explanation": {
                                "type": "string",
                                "description": "本次调整清单的简短原因。",
                            },
                        },
                        "required": ["objective", "items", "explanation"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name != "update_task_list":
            return ToolResult(False, error=f"未知工具: {name}")
        unknown = set(arguments) - {"objective", "items", "explanation"}
        if unknown:
            return ToolResult(False, error=f"未知参数: {', '.join(sorted(unknown))}")
        explanation = arguments.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            return ToolResult(False, error="explanation 必须是非空字符串")
        if len(explanation.strip()) > 500:
            return ToolResult(False, error="explanation 不能超过 500 个字符")
        before = self.state.snapshot()
        try:
            snapshot = self.state.replace(
                arguments.get("objective"), arguments.get("items")
            )
            self.on_update(snapshot)
        except (TypeError, ValueError) as exc:
            self.state.restore(before)
            return ToolResult(False, error=str(exc))
        except Exception as exc:
            self.state.restore(before)
            return ToolResult(False, error=f"任务清单保存失败: {exc}")
        return ToolResult(
            True,
            output=json.dumps(
                {
                    "objective": snapshot["objective"],
                    "items": snapshot["items"],
                    "progress": {
                        "completed": snapshot["completed"],
                        "total": snapshot["total"],
                        "blocked": snapshot["blocked"],
                    },
                    "explanation": " ".join(explanation.split()),
                },
                ensure_ascii=False,
            ),
        )
