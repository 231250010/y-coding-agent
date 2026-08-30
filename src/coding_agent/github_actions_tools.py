from __future__ import annotations

import json
from typing import Any, Callable

from .changes import ChangeSet
from .github_actions_service import GitHubActionsError, GitHubActionsService
from .safety import RiskLevel
from .tools import ApprovalCallback, MAX_TOOL_OUTPUT, ToolResult


def _schema(
    name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


class GitHubActionsToolProvider:
    def __init__(
        self,
        service: GitHubActionsService,
        *,
        approver: ApprovalCallback | None = None,
        max_output: int = MAX_TOOL_OUTPUT,
    ) -> None:
        self.service = service
        self.approver = approver or (lambda _command, _risk, _reason: False)
        self.max_output = max_output
        run_id = {"type": "integer", "minimum": 1}
        commit = {
            "type": "string",
            "minLength": 7,
            "maxLength": 40,
            "description": "可选 Commit SHA；省略时使用 HEAD",
        }
        self._schemas = [
            _schema(
                "github_actions_status",
                "查询当前或指定 Commit 最新的 GitHub Actions 工作流状态。",
                {
                    "commit": commit,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            ),
            _schema(
                "github_actions_failed_logs",
                "读取指定 GitHub Actions 运行的失败步骤日志并进行脱敏和截断。",
                {
                    "run_id": run_id,
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 50000,
                        "default": 12000,
                    },
                },
                ["run_id"],
            ),
            _schema(
                "github_actions_rerun_failed",
                "重新运行指定 Actions run 的失败任务；始终需要人工确认。",
                {"run_id": run_id},
                ["run_id"],
            ),
        ]
        self._schemas_by_name = {
            item["function"]["name"]: item["function"]["parameters"] for item in self._schemas
        }
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "github_actions_status": lambda args: self.service.status(
                args.get("commit"), limit=args.get("limit", 20)
            ),
            "github_actions_failed_logs": lambda args: self.service.failed_logs(
                args["run_id"], max_chars=args.get("max_chars", 12000)
            ),
            "github_actions_rerun_failed": lambda args: self.service.rerun_failed(args["run_id"]),
        }

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(False, error=f"未知工具: {name}")
        validation = self._validate(arguments, self._schemas_by_name[name])
        if validation:
            return ToolResult(False, error=validation)
        if name == "github_actions_rerun_failed":
            run_id = arguments["run_id"]
            reason = f"人工确认：重新运行 GitHub Actions run {run_id} 的失败任务"
            if not self.approver(f"gh run rerun {run_id} --failed", RiskLevel.REVIEW, reason):
                return ToolResult(False, error=f"用户未批准 GitHub Actions 重跑: {reason}")
        try:
            payload = json.dumps(handler(arguments), ensure_ascii=False, indent=2)
            if len(payload) > self.max_output:
                payload = payload[: self.max_output] + "\n...输出已截断"
            return ToolResult(True, output=payload, changes=ChangeSet())
        except GitHubActionsError as exc:
            output = exc.output[: self.max_output]
            return ToolResult(False, output=output, error=f"{exc.code}: {exc}")

    @staticmethod
    def _validate(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
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
            if spec.get("type") == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    return f"参数 {key} 必须是 integer"
                if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                    return f"参数 {key} 超出允许范围"
            elif spec.get("type") == "string":
                if not isinstance(value, str):
                    return f"参数 {key} 必须是 string"
                if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", len(value)):
                    return f"参数 {key} 长度无效"
        return None
