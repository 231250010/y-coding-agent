from __future__ import annotations

import json
from typing import Any, Callable

from .changes import ChangeSet, ConversationChangeTracker
from .devops_service import DevOpsOperationError, DevOpsService
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


class DevOpsToolProvider:
    """Structured Docker Compose development, deployment, and operations tools."""

    _MUTATIONS = frozenset(
        {
            "compose_build",
            "compose_pull",
            "compose_deploy",
            "compose_release",
            "compose_rollback",
            "compose_restart",
            "compose_stop",
        }
    )

    def __init__(
        self,
        service: DevOpsService,
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
        environment = {
            "type": "string",
            "description": "coding-agent.toml 中配置的环境名称；省略时使用默认环境",
            "minLength": 1,
            "maxLength": 80,
        }
        services = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 100,
            "description": "可选服务列表；空数组表示全部服务",
        }
        common = {"environment": environment}
        writes = {"environment": environment, "services": services}
        version = {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "发布版本，例如 v1.4.0 或 2026.08.30",
        }
        self._schemas = [
            self._schema("devops_inspect", "识别项目技术栈、Compose 文件和已配置部署环境。"),
            self._schema(
                "compose_preflight",
                "只读检查 Docker Engine、Compose 版本、配置有效性和服务列表。",
                _object_schema(common),
            ),
            self._schema(
                "compose_status",
                "查询指定环境中的 Docker Compose 服务状态。",
                _object_schema(common),
            ),
            self._schema(
                "compose_logs",
                "读取指定环境的有界 Docker Compose 日志。",
                _object_schema(
                    {
                        **common,
                        "service": {"type": "string", "minLength": 1, "maxLength": 120},
                        "tail": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                    }
                ),
            ),
            self._schema(
                "compose_build",
                "构建指定环境的 Compose 镜像。执行 Dockerfile，必须经过审批。",
                _object_schema(writes),
            ),
            self._schema(
                "compose_pull",
                "拉取指定环境的 Compose 镜像。会访问镜像仓库，必须经过审批。",
                _object_schema(writes),
            ),
            self._schema(
                "compose_deploy",
                "校验配置后执行 Compose 后台构建部署，等待健康状态收敛并运行应用探针。",
                _object_schema(writes),
            ),
            self._schema(
                "compose_release",
                "执行 Git/检查门禁后发布命名版本，记录来源证据和不可变镜像 ID。",
                _object_schema({**writes, "version": version}, ["version"]),
            ),
            self._schema(
                "compose_releases",
                "只读查询发布版本、当前活动版本和回滚审计记录。",
                _object_schema(
                    {
                        **common,
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 20,
                        },
                    }
                ),
            ),
            self._schema(
                "compose_rollback_plan",
                "只读生成十分钟有效的一次性回滚预览；执行回滚前必须先调用。",
                _object_schema({**common, "version": version}, ["version"]),
            ),
            self._schema(
                "compose_rollback",
                "执行一次性回滚计划。无论权限模式如何，都必须由用户人工确认。",
                _object_schema(
                    {
                        "plan_id": {
                            "type": "string",
                            "minLength": 32,
                            "maxLength": 32,
                        }
                    },
                    ["plan_id"],
                ),
            ),
            self._schema(
                "compose_verify",
                "根据容器运行状态和 Compose health 状态验证部署结果。",
                _object_schema(common),
            ),
            self._schema(
                "compose_restart",
                "重启指定环境的 Compose 服务，必须经过审批。",
                _object_schema(writes),
            ),
            self._schema(
                "compose_stop",
                "停止指定环境的 Compose 服务但不删除容器或数据卷，必须经过审批。",
                _object_schema(writes),
            ),
        ]
        self._schema_by_name = {
            item["function"]["name"]: item["function"]["parameters"] for item in self._schemas
        }
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "devops_inspect": lambda _args: self.service.inspect(),
            "compose_preflight": lambda args: self.service.preflight(args.get("environment")),
            "compose_status": lambda args: self.service.status(args.get("environment")),
            "compose_logs": lambda args: self.service.logs(
                args.get("environment"), args.get("service"), args.get("tail", 200)
            ),
            "compose_build": lambda args: self.service.build(
                args.get("environment"), args.get("services")
            ),
            "compose_pull": lambda args: self.service.pull(
                args.get("environment"), args.get("services")
            ),
            "compose_deploy": lambda args: self.service.deploy(
                args.get("environment"), args.get("services")
            ),
            "compose_release": lambda args: self.service.release(
                args["version"], args.get("environment"), args.get("services")
            ),
            "compose_releases": lambda args: self.service.releases(
                args.get("environment"), args.get("limit", 20)
            ),
            "compose_rollback_plan": lambda args: self.service.rollback_plan(
                args["version"], args.get("environment")
            ),
            "compose_rollback": lambda args: self.service.rollback(args["plan_id"]),
            "compose_verify": lambda args: self.service.verify(args.get("environment")),
            "compose_restart": lambda args: self.service.restart(
                args.get("environment"), args.get("services")
            ),
            "compose_stop": lambda args: self.service.stop(
                args.get("environment"), args.get("services")
            ),
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

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(False, error=f"未知工具: {name}")
        validation_error = self._validate(arguments, self._schema_by_name[name])
        if validation_error:
            return ToolResult(False, error=validation_error)
        try:
            release_preview = (
                self.service.release_preview(arguments.get("environment"))
                if name == "compose_release"
                else None
            )
            approval_error, allow_review_checks = self._approve(
                name, arguments, release_preview
            )
            if approval_error:
                return ToolResult(False, error=approval_error)
            if name == "compose_release":
                assert release_preview is not None
                data = self.service.release(
                    arguments["version"],
                    arguments.get("environment"),
                    arguments.get("services"),
                    expected_policy_digest=release_preview["policy_digest"],
                    allow_review_checks=allow_review_checks,
                )
            else:
                data = handler(arguments)
            output = json.dumps(data, ensure_ascii=False, indent=2)
            if len(output) > self.max_output:
                output = output[: self.max_output] + "\n...输出已截断"
            return ToolResult(True, output=output, changes=ChangeSet())
        except DevOpsOperationError as exc:
            output = exc.output
            if len(output) > self.max_output:
                output = output[: self.max_output] + "\n...输出已截断"
            return ToolResult(False, output=output, error=f"{exc.code}: {exc}")

    def _approve(
        self,
        name: str,
        arguments: dict[str, Any],
        release_preview: dict[str, Any] | None = None,
    ) -> tuple[str | None, bool]:
        if name == "compose_release":
            assert release_preview is not None
            return self._approve_release(arguments, release_preview)
        if name == "compose_rollback":
            preview = self.service.rollback_preview(arguments["plan_id"])
            label = f"compose_rollback {json.dumps(preview, ensure_ascii=False, separators=(',', ':'))}"
            reason = (
                f"人工确认：将 {preview['environment']} 从 "
                f"{preview.get('from_version') or '未记录版本'} 回滚到 {preview['target_version']}；"
                "会重新标记镜像并重建服务，不会自动回滚数据库"
            )
            if self.approver(label, RiskLevel.REVIEW, reason):
                return None, False
            return f"用户未批准回滚操作: {reason}", False
        if name not in self._MUTATIONS or self.approval_mode == "full":
            return None, False
        environment = arguments.get("environment") or "默认环境"
        label = f"{name} {json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))}"
        reason = f"DevOps 操作会修改 {environment} 的镜像、容器或服务状态"
        if self.approver(label, RiskLevel.REVIEW, reason):
            return None, False
        return f"用户未批准 DevOps 操作: {reason}", False

    def _approve_release(
        self, arguments: dict[str, Any], preview: dict[str, Any]
    ) -> tuple[str | None, bool]:
        checks = preview.get("checks") if isinstance(preview.get("checks"), list) else []
        denied = [item for item in checks if item.get("risk") == RiskLevel.DENY.value]
        if denied:
            names = "、".join(str(item.get("name") or "未命名检查") for item in denied)
            return f"发布门禁包含禁止命令，不能执行: {names}", False
        review = [item for item in checks if item.get("risk") == RiskLevel.REVIEW.value]
        config_changed = bool(
            self.change_tracker
            and "coding-agent.toml" in self.change_tracker.changes
        )
        requires_approval = self.approval_mode != "full" or bool(review) or config_changed
        if not requires_approval:
            return None, False

        display = {
            "version": arguments["version"],
            "environment": preview["environment"],
            "require_git": preview.get("require_git", False),
            "require_clean_worktree": preview.get("require_clean_worktree", False),
            "github_actions": preview.get("github_actions", {}),
            "config_changed_in_task": config_changed,
            "checks": checks,
        }
        label = f"compose_release {json.dumps(display, ensure_ascii=False, separators=(',', ':'))}"
        command_text = "；".join(
            f"{item.get('name')}: {json.dumps(item.get('command'), ensure_ascii=False)}"
            for item in checks
        ) or "无额外检查命令"
        warnings = []
        if config_changed:
            warnings.append("本次任务修改了 coding-agent.toml")
        if review:
            warnings.append("包含未列入安全清单的命令")
        suffix = f"；注意：{'；'.join(warnings)}" if warnings else ""
        reason = (
            f"人工确认发布 {arguments['version']} 到 {preview['environment']}；"
            f"门禁命令：{command_text}{suffix}"
        )
        if self.approver(label, RiskLevel.REVIEW, reason):
            return None, bool(review)
        return f"用户未批准发布操作: {reason}", False

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
            kind = spec.get("type")
            if kind == "string":
                if not isinstance(value, str):
                    return f"参数 {key} 必须是 string"
                if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", len(value)):
                    return f"参数 {key} 长度无效"
            elif kind == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    return f"参数 {key} 必须是 integer"
                if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                    return f"参数 {key} 超出允许范围"
            elif kind == "array":
                if not isinstance(value, list) or len(value) > spec.get("maxItems", len(value)):
                    return f"参数 {key} 必须是有界数组"
                if any(not isinstance(item, str) for item in value):
                    return f"参数 {key} 的每一项必须是 string"
        return None
