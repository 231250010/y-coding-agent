from __future__ import annotations

import json
from typing import Any, Callable

from .changes import Capture, ChangeSet, ConversationChangeTracker
from .git_service import GitOperationError, GitService
from .permissions import normalize_permission_mode
from .safety import RiskLevel
from .tools import ApprovalCallback, MAX_TOOL_OUTPUT, ToolResult


def _object_schema(
    properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


class GitToolProvider:
    """Validate, approve, and execute the narrow structured Git surface."""

    _QUERY_TOOLS = frozenset({"git_status", "git_diff", "git_log", "git_branches"})
    _LOCAL_WRITE_TOOLS = frozenset(
        {"git_create_branch", "git_stage", "git_unstage", "git_commit"}
    )
    _REMOTE_WRITE_TOOLS = frozenset({"git_pull", "git_push"})

    def __init__(
        self,
        service: GitService,
        *,
        approval_mode: str = "risk",
        approver: ApprovalCallback | None = None,
        change_tracker: ConversationChangeTracker | None = None,
        max_output: int = MAX_TOOL_OUTPUT,
    ) -> None:
        self.service = service
        self.approval_mode = normalize_permission_mode(approval_mode)
        self.approver = approver or (lambda _command, _risk, _reason: False)
        self.change_tracker = change_tracker
        self.max_output = max_output
        path = {"type": "string", "description": "相对于当前工作目录的 Git 路径"}
        paths = {"type": "array", "items": path, "minItems": 1, "maxItems": 200}
        self._schemas = [
            self._schema("git_status", "返回当前分支、上游、领先/落后和文件状态。"),
            self._schema(
                "git_diff",
                "返回工作区或暂存区的 Git Diff，可限定单个路径。",
                _object_schema(
                    {
                        "scope": {
                            "type": "string",
                            "enum": ["workspace", "staged"],
                            "default": "workspace",
                        },
                        "path": path,
                    }
                ),
            ),
            self._schema(
                "git_log",
                "返回最近的 Git 提交记录。",
                _object_schema(
                    {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}
                ),
            ),
            self._schema("git_branches", "列出本地分支、当前分支和上游。"),
            self._schema(
                "git_create_branch",
                "创建并切换到一个新的本地分支。",
                _object_schema({"name": {"type": "string", "minLength": 1, "maxLength": 240}}, ["name"]),
            ),
            self._schema("git_stage", "暂存明确列出的工作区路径。", _object_schema({"paths": paths}, ["paths"])),
            self._schema(
                "git_unstage",
                "取消暂存明确列出的路径，但保留工作区内容。",
                _object_schema({"paths": paths}, ["paths"]),
            ),
            self._schema(
                "git_commit",
                "提交当前暂存区内容。不会自动暂存文件。",
                _object_schema(
                    {"message": {"type": "string", "minLength": 1, "maxLength": 500}}, ["message"]
                ),
            ),
            self._schema("git_pull", "使用 fast-forward-only 拉取当前分支，不执行自动合并。"),
            self._schema("git_push", "推送当前分支；不支持强制推送或任意 refspec。"),
        ]
        self._schema_by_name = {item["function"]["name"]: item["function"]["parameters"] for item in self._schemas}
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "git_status": lambda _args: self.service.status(),
            "git_diff": lambda args: self.service.diff(args.get("scope", "workspace"), args.get("path")),
            "git_log": lambda args: self.service.log(args.get("limit", 20)),
            "git_branches": lambda _args: self.service.branches(),
            "git_create_branch": lambda args: self.service.create_branch(args["name"]),
            "git_stage": lambda args: self.service.stage(args["paths"]),
            "git_unstage": lambda args: self.service.unstage(args["paths"]),
            "git_commit": lambda args: self.service.commit(args["message"]),
            "git_pull": lambda _args: self.service.pull(),
            "git_push": lambda _args: self.service.push(),
        }

    @staticmethod
    def _schema(name: str, description: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters or _object_schema(),
            },
        }

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def can_run_parallel(self, name: str, _arguments: dict[str, Any]) -> bool:
        return name in self._QUERY_TOOLS

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(False, error=f"未知工具: {name}")
        validation_error = self._validate(arguments, self._schema_by_name[name])
        if validation_error:
            return ToolResult(False, error=validation_error)
        approval_error = self._approve(name, arguments)
        if approval_error:
            return ToolResult(False, error=approval_error)

        capture: Capture | None = None
        if name == "git_pull" and self.change_tracker is not None:
            capture = self.change_tracker.capture_workspace()
        try:
            data = handler(arguments)
            output = json.dumps(data, ensure_ascii=False, indent=2)
            if len(output) > self.max_output:
                output = output[: self.max_output] + "\n...输出已截断"
            changes = self.change_tracker.finish(capture) if capture and self.change_tracker else None
            return ToolResult(True, output=output, changes=changes or ChangeSet())
        except GitOperationError as exc:
            changes = self.change_tracker.finish(capture) if capture and self.change_tracker else None
            output = exc.output
            if len(output) > self.max_output:
                output = output[: self.max_output] + "\n...输出已截断"
            return ToolResult(
                False,
                output=output,
                error=f"{exc.code}: {exc}",
                changes=changes or ChangeSet(),
            )

    def _approve(self, name: str, arguments: dict[str, Any]) -> str | None:
        asks = (
            name in self._LOCAL_WRITE_TOOLS and self.approval_mode == "request"
        ) or (
            name in self._REMOTE_WRITE_TOOLS and self.approval_mode != "full"
        )
        if not asks:
            return None
        label = f"{name} {json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))}"
        reason = "Git 操作会修改本地仓库" if name in self._LOCAL_WRITE_TOOLS else "Git 操作会访问远端并修改仓库状态"
        if self.approver(label, RiskLevel.REVIEW, reason):
            return None
        return f"用户未批准 Git 操作: {reason}"

    @classmethod
    def _validate(cls, arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
        if not isinstance(arguments, dict):
            return "工具参数必须是 JSON 对象"
        properties = schema.get("properties", {})
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            return f"未知参数: {', '.join(unknown)}"
        for required in schema.get("required", []):
            if required not in arguments:
                return f"缺少参数: {required}"
        for key, value in arguments.items():
            spec = properties[key]
            kind = spec.get("type")
            if kind == "string":
                if not isinstance(value, str):
                    return f"参数 {key} 必须是 string"
                if len(value) < spec.get("minLength", 0):
                    return f"参数 {key} 不能为空"
                if len(value) > spec.get("maxLength", len(value)):
                    return f"参数 {key} 过长"
                if "enum" in spec and value not in spec["enum"]:
                    return f"参数 {key} 不在允许范围内"
            elif kind == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    return f"参数 {key} 必须是 integer"
                if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                    return f"参数 {key} 超出允许范围"
            elif kind == "array":
                if not isinstance(value, list):
                    return f"参数 {key} 必须是 array"
                if len(value) < spec.get("minItems", 0) or len(value) > spec.get("maxItems", len(value)):
                    return f"参数 {key} 数量超出允许范围"
                item_spec = spec.get("items", {})
                if item_spec.get("type") == "string" and any(not isinstance(item, str) for item in value):
                    return f"参数 {key} 的每一项必须是 string"
        return None
