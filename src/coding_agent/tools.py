from __future__ import annotations

import fnmatch
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .changes import Capture, ChangeSet, ConversationChangeTracker
from .permissions import normalize_permission_mode
from .safety import CommandPolicy, RiskLevel


MAX_FILE_CHARS = 200_000
MAX_TOOL_OUTPUT = 16_000
MAX_BATCH_FILES = 50
MAX_BATCH_CHARS = 1_000_000
MAX_BATCH_ROLLBACK_BYTES = 10_000_000
ApprovalCallback = Callable[[str, RiskLevel, str], bool]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: str = ""
    error: str | None = None
    changes: ChangeSet = field(default_factory=ChangeSet)

    def to_message(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.output:
            payload["output"] = self.output
        if self.error:
            payload["error"] = self.error
        return json.dumps(payload, ensure_ascii=False)


class PathGuard:
    def __init__(self, workspace: Path, *, allow_outside: bool = False) -> None:
        self.workspace = workspace.resolve()
        self.allow_outside = allow_outside

    def resolve(self, user_path: str, *, must_exist: bool = False) -> Path:
        candidate_input = Path(user_path)
        candidate = candidate_input if candidate_input.is_absolute() else self.workspace / candidate_input
        resolved = candidate.resolve(strict=False)
        if not self.allow_outside or not candidate_input.is_absolute():
            try:
                resolved.relative_to(self.workspace)
            except ValueError as exc:
                raise ValueError(f"路径超出工作区: {user_path}") from exc

        # Existing parents can contain symlinks; resolve() above follows them.
        if must_exist and not resolved.exists():
            raise ValueError(f"路径不存在: {user_path}")
        return resolved


@dataclass(slots=True)
class LocalTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    _PARALLEL_SAFE_TOOLS = frozenset({"list_files", "read_file", "search_text"})
    _FILE_MUTATION_TOOLS = frozenset(
        {"write_file", "replace_text", "batch_write_files", "batch_replace_text"}
    )

    def __init__(
        self,
        workspace: Path | None,
        *,
        approver: ApprovalCallback | None = None,
        is_cancelled: CancelCallback | None = None,
        approval_mode: str = "risk",
        max_output: int = MAX_TOOL_OUTPUT,
        change_tracker: ConversationChangeTracker | None = None,
    ) -> None:
        self.approver = approver or (lambda _command, _risk, _reason: False)
        self.is_cancelled = is_cancelled or (lambda: False)
        self.approval_mode = normalize_permission_mode(approval_mode)
        self.max_output = max_output
        self.change_tracker = change_tracker
        if workspace is None:
            self.workspace = None
            self.guard = None
            self.policy = None
            self._tools = {}
            return
        self.workspace = workspace.resolve()
        self.guard = PathGuard(self.workspace, allow_outside=self.approval_mode == "full")
        self.policy = CommandPolicy(self.workspace)
        self._tools = {tool.name: tool for tool in self._build_tools()}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def can_run_parallel(self, name: str, _arguments: dict[str, Any]) -> bool:
        return name in self._PARALLEL_SAFE_TOOLS

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if self.workspace is None:
            return ToolResult(False, error="当前对话尚未选择工作目录")
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, error=f"未知工具: {name}")
        validation_error = self._validate(arguments, tool.parameters)
        if validation_error:
            return ToolResult(False, error=validation_error)

        if name in self._FILE_MUTATION_TOOLS and self.approval_mode == "request":
            paths = self._mutation_paths(name, arguments)
            target = ", ".join(paths[:5])
            if len(paths) > 5:
                target += f" 等 {len(paths)} 个文件"
            if not self.approver(
                f"{name} {target}",
                RiskLevel.REVIEW,
                "请求批准模式下，修改文件需要确认",
            ):
                return ToolResult(False, error="用户未批准文件修改")

        capture: Capture | None = None
        try:
            mutation_paths = self._mutation_paths(name, arguments)
            tracked_paths = [path for path in mutation_paths if self._inside_workspace(path)]
            if self.change_tracker and tracked_paths:
                capture = self.change_tracker.capture_paths(tracked_paths)
            elif self.change_tracker and name in {"run_command", "run_process"}:
                capture = self.change_tracker.capture_workspace()
            result = tool.handler(arguments)
        except (OSError, UnicodeError, ValueError) as exc:
            result = ToolResult(False, error=str(exc))
        except Exception as exc:
            result = ToolResult(False, error=f"工具执行异常: {type(exc).__name__}: {exc}")
        if capture is not None and self.change_tracker is not None:
            try:
                changes = self.change_tracker.finish(capture)
            except (OSError, UnicodeError, ValueError) as exc:
                changes = ChangeSet(warning=f"文件改动追踪失败: {exc}")
            result = replace(result, changes=changes)
        return result

    @staticmethod
    def _validate(arguments: Any, schema: dict[str, Any]) -> str | None:
        if not isinstance(arguments, dict):
            return "工具参数必须是 JSON 对象"
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in arguments:
                return f"缺少必需参数: {required}"
        if schema.get("additionalProperties") is False:
            unknown = set(arguments) - set(properties)
            if unknown:
                return f"未知参数: {', '.join(sorted(unknown))}"
        python_types = {
            "string": str,
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for key, value in arguments.items():
            expected = properties.get(key, {}).get("type")
            expected_type = python_types.get(expected)
            if expected_type and (not isinstance(value, expected_type) or (expected == "integer" and isinstance(value, bool))):
                return f"参数 {key} 必须是 {expected}"
            minimum = properties.get(key, {}).get("minimum")
            maximum = properties.get(key, {}).get("maximum")
            if isinstance(value, int) and minimum is not None and value < minimum:
                return f"参数 {key} 不能小于 {minimum}"
            if isinstance(value, int) and maximum is not None and value > maximum:
                return f"参数 {key} 不能大于 {maximum}"
            minimum_items = properties.get(key, {}).get("minItems")
            maximum_items = properties.get(key, {}).get("maxItems")
            if isinstance(value, list) and minimum_items is not None and len(value) < minimum_items:
                return f"参数 {key} 至少需要 {minimum_items} 项"
            if isinstance(value, list) and maximum_items is not None and len(value) > maximum_items:
                return f"参数 {key} 不能超过 {maximum_items} 项"
        return None

    def _build_tools(self) -> list[LocalTool]:
        path_description = "相对于工作区的路径"
        if self.approval_mode == "full":
            path_description += "；完全访问权限下也可使用绝对路径"
        common_path = {"type": "string", "description": path_description}
        return [
            LocalTool(
                "list_files",
                "列出工作区目录中的文件。",
                {
                    "type": "object",
                    "properties": {
                        "path": {**common_path, "default": "."},
                        "pattern": {"type": "string", "default": "**/*"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                    },
                    "additionalProperties": False,
                },
                self._list_files,
            ),
            LocalTool(
                "read_file",
                "读取 UTF-8 文本文件，可指定起始行和最大行数。",
                {
                    "type": "object",
                    "properties": {
                        "path": common_path,
                        "start_line": {"type": "integer", "minimum": 1, "default": 1},
                        "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 400},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                self._read_file,
            ),
            LocalTool(
                "search_text",
                "在工作区文件中搜索文本，返回文件名、行号和匹配行。",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {**common_path, "default": "."},
                        "glob": {"type": "string", "default": "*"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                self._search_text,
            ),
            LocalTool(
                "write_file",
                "创建或完整覆盖工作区中的 UTF-8 文本文件。",
                {
                    "type": "object",
                    "properties": {"path": common_path, "content": {"type": "string"}},
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                self._write_file,
            ),
            LocalTool(
                "replace_text",
                "在文本文件中精确替换内容；默认要求旧文本只出现一次。",
                {
                    "type": "object",
                    "properties": {
                        "path": common_path,
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
                self._replace_text,
            ),
            LocalTool(
                "batch_write_files",
                "批量创建或完整覆盖多个 UTF-8 文本文件；全部操作预检成功后才写入，提交失败时回滚。",
                {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_BATCH_FILES,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": common_path,
                                    "content": {"type": "string"},
                                },
                                "required": ["path", "content"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["files"],
                    "additionalProperties": False,
                },
                self._batch_write_files,
            ),
            LocalTool(
                "batch_replace_text",
                "在多个 UTF-8 文本文件中执行精确替换；全部匹配预检成功后才写入。",
                {
                    "type": "object",
                    "properties": {
                        "replacements": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_BATCH_FILES,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": common_path,
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                    "replace_all": {"type": "boolean", "default": False},
                                },
                                "required": ["path", "old_text", "new_text"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["replacements"],
                    "additionalProperties": False,
                },
                self._batch_replace_text,
            ),
            LocalTool(
                "run_process",
                "使用参数数组和 shell=False 在工作区执行测试、构建或检查；优先用于可验证命令。",
                {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 30,
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "default": 60,
                        },
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                self._run_process,
            ),
            LocalTool(
                "run_command",
                "在工作区中执行本地 shell 命令，返回退出码和截断后的输出。",
                {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 60},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                self._run_command,
            ),
        ]

    @staticmethod
    def _mutation_paths(name: str, arguments: dict[str, Any]) -> list[str]:
        if name in {"write_file", "replace_text"}:
            path = arguments.get("path")
            return [path] if isinstance(path, str) else []
        if name == "batch_write_files":
            key = "files"
        elif name == "batch_replace_text":
            key = "replacements"
        else:
            return []
        items = arguments.get(key)
        if not isinstance(items, list):
            return []
        return [str(item["path"]) for item in items if isinstance(item, dict) and isinstance(item.get("path"), str)]

    def _inside_workspace(self, user_path: str) -> bool:
        if self.workspace is None or self.guard is None:
            return False
        try:
            self.guard.resolve(user_path).relative_to(self.workspace)
        except ValueError:
            return False
        return True

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output:
            return text
        omitted = len(text) - self.max_output
        return f"{text[:self.max_output]}\n... [已截断 {omitted} 个字符]"

    def _list_files(self, args: dict[str, Any]) -> ToolResult:
        base = self.guard.resolve(args.get("path", "."), must_exist=True)
        if not base.is_dir():
            return ToolResult(False, error="path 不是目录")
        pattern = args.get("pattern", "**/*")
        limit = args.get("max_results", 200)
        results: list[str] = []
        for item in sorted(base.rglob("*")):
            if ".git" in item.parts:
                continue
            relative_to_base = item.relative_to(base).as_posix()
            if pattern not in {"*", "**/*"} and not fnmatch.fnmatch(relative_to_base, pattern):
                continue
            kind = "/" if item.is_dir() else ""
            results.append(f"{self._display_path(item)}{kind}")
            if len(results) >= limit:
                break
        suffix = "\n... [结果达到上限]" if len(results) >= limit else ""
        return ToolResult(True, "\n".join(results) + suffix)

    def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self.guard.resolve(args["path"], must_exist=True)
        if not path.is_file():
            return ToolResult(False, error="path 不是文件")
        start = args.get("start_line", 1)
        maximum = args.get("max_lines", 400)
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        selected = lines[start - 1 : start - 1 + maximum]
        rendered = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start=start))
        if start - 1 + maximum < len(lines):
            rendered += f"\n... [共 {len(lines)} 行，输出已截断]"
        return ToolResult(True, self._truncate(rendered))

    def _search_text(self, args: dict[str, Any]) -> ToolResult:
        query = args["query"]
        if not query:
            return ToolResult(False, error="query 不能为空")
        base = self.guard.resolve(args.get("path", "."), must_exist=True)
        glob = args.get("glob", "*")
        limit = args.get("max_results", 100)
        rg = shutil.which("rg")
        if rg:
            command = [rg, "--line-number", "--color", "never", "--glob", glob, "--", query, str(base)]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if completed.returncode not in {0, 1}:
                return ToolResult(False, error=self._truncate(completed.stderr.strip()))
            lines = completed.stdout.splitlines()[:limit]
            return ToolResult(True, self._truncate("\n".join(lines) or "未找到匹配"))

        matches: list[str] = []
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            if not path.is_file() or ".git" in path.parts or not fnmatch.fnmatch(path.name, glob):
                continue
            try:
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    if query in line:
                        matches.append(f"{self._display_path(path)}:{number}:{line}")
                        if len(matches) >= limit:
                            return ToolResult(True, self._truncate("\n".join(matches) + "\n... [结果达到上限]"))
            except (UnicodeDecodeError, OSError):
                continue
        return ToolResult(True, self._truncate("\n".join(matches) or "未找到匹配"))

    def _write_file(self, args: dict[str, Any]) -> ToolResult:
        content = args["content"]
        if len(content) > MAX_FILE_CHARS:
            return ToolResult(False, error=f"单次写入不能超过 {MAX_FILE_CHARS} 个字符")
        path = self.guard.resolve(args["path"])
        if path.exists() and not path.is_file():
            return ToolResult(False, error="目标路径不是文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(True, f"已写入 {self._display_path(path)}（{len(content)} 个字符）")

    def _replace_text(self, args: dict[str, Any]) -> ToolResult:
        path = self.guard.resolve(args["path"], must_exist=True)
        if not path.is_file():
            return ToolResult(False, error="path 不是文件")
        old = args["old_text"]
        if not old:
            return ToolResult(False, error="old_text 不能为空")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return ToolResult(False, error="未找到 old_text")
        replace_all = args.get("replace_all", False)
        if count > 1 and not replace_all:
            return ToolResult(False, error=f"old_text 出现 {count} 次；请提供更精确文本或启用 replace_all")
        updated = text.replace(old, args["new_text"], -1 if replace_all else 1)
        path.write_text(updated, encoding="utf-8")
        replacements = count if replace_all else 1
        return ToolResult(True, f"已在 {self._display_path(path)} 中替换 {replacements} 处")

    def _batch_write_files(self, args: dict[str, Any]) -> ToolResult:
        raw_files = args.get("files")
        self._validate_batch_collection(raw_files, "files")
        updates: list[tuple[Path, str]] = []
        details: list[dict[str, Any]] = []
        seen: set[Path] = set()
        total_chars = 0
        for index, item in enumerate(raw_files):
            self._validate_batch_item(item, index, {"path", "content"}, {"path", "content"})
            path_value = item["path"]
            content = item["content"]
            if not isinstance(path_value, str) or not path_value:
                raise ValueError(f"files[{index}].path 必须是非空字符串")
            if not isinstance(content, str):
                raise ValueError(f"files[{index}].content 必须是字符串")
            if len(content) > MAX_FILE_CHARS:
                raise ValueError(
                    f"files[{index}].content 不能超过 {MAX_FILE_CHARS} 个字符"
                )
            total_chars += len(content)
            if total_chars > MAX_BATCH_CHARS:
                raise ValueError(f"批量写入总内容不能超过 {MAX_BATCH_CHARS} 个字符")
            path = self.guard.resolve(path_value)
            if path in seen:
                raise ValueError(f"批量操作包含重复路径: {path_value}")
            if path.exists() and not path.is_file():
                raise ValueError(f"目标路径不是文件: {path_value}")
            seen.add(path)
            updates.append((path, content))
            details.append(
                {
                    "path": self._display_path(path),
                    "action": "updated" if path.exists() else "created",
                    "characters": len(content),
                }
            )

        self._commit_batch(updates)
        return ToolResult(True, self._batch_output(details))

    def _batch_replace_text(self, args: dict[str, Any]) -> ToolResult:
        raw_replacements = args.get("replacements")
        self._validate_batch_collection(raw_replacements, "replacements")
        updates: list[tuple[Path, str]] = []
        details: list[dict[str, Any]] = []
        seen: set[Path] = set()
        total_chars = 0
        allowed = {"path", "old_text", "new_text", "replace_all"}
        required = {"path", "old_text", "new_text"}
        for index, item in enumerate(raw_replacements):
            self._validate_batch_item(item, index, allowed, required)
            path_value = item["path"]
            old_text = item["old_text"]
            new_text = item["new_text"]
            replace_all = item.get("replace_all", False)
            if not isinstance(path_value, str) or not path_value:
                raise ValueError(f"replacements[{index}].path 必须是非空字符串")
            if not isinstance(old_text, str) or not old_text:
                raise ValueError(f"replacements[{index}].old_text 必须是非空字符串")
            if not isinstance(new_text, str):
                raise ValueError(f"replacements[{index}].new_text 必须是字符串")
            if not isinstance(replace_all, bool):
                raise ValueError(f"replacements[{index}].replace_all 必须是 boolean")
            path = self.guard.resolve(path_value, must_exist=True)
            if path in seen:
                raise ValueError(f"批量操作包含重复路径: {path_value}")
            if not path.is_file():
                raise ValueError(f"path 不是文件: {path_value}")
            text = path.read_text(encoding="utf-8")
            if len(text) > MAX_FILE_CHARS:
                raise ValueError(f"文件超过批量替换上限: {path_value}")
            count = text.count(old_text)
            if count == 0:
                raise ValueError(f"未在 {path_value} 中找到 old_text")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"{path_value} 中 old_text 出现 {count} 次；请提供更精确文本或启用 replace_all"
                )
            updated = text.replace(old_text, new_text, -1 if replace_all else 1)
            if len(updated) > MAX_FILE_CHARS:
                raise ValueError(f"替换后的文件超过 {MAX_FILE_CHARS} 个字符: {path_value}")
            total_chars += len(updated)
            if total_chars > MAX_BATCH_CHARS:
                raise ValueError(f"批量替换总内容不能超过 {MAX_BATCH_CHARS} 个字符")
            seen.add(path)
            updates.append((path, updated))
            details.append(
                {
                    "path": self._display_path(path),
                    "replacements": count if replace_all else 1,
                }
            )

        self._commit_batch(updates)
        return ToolResult(True, self._batch_output(details))

    def _batch_output(self, details: list[dict[str, Any]]) -> str:
        payload: dict[str, Any] = {"count": len(details), "files": details}
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(rendered) <= self.max_output:
            return rendered
        kept: list[dict[str, Any]] = []
        for detail in details:
            candidate = {
                "count": len(details),
                "files": [*kept, detail],
                "omitted": len(details) - len(kept) - 1,
            }
            if len(json.dumps(candidate, ensure_ascii=False, indent=2)) > self.max_output:
                break
            kept.append(detail)
        compact = {
            "count": len(details),
            "files": kept,
            "omitted": len(details) - len(kept),
        }
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _validate_batch_collection(value: Any, name: str) -> None:
        if not isinstance(value, list):
            raise ValueError(f"参数 {name} 必须是 array")
        if not value:
            raise ValueError(f"参数 {name} 至少需要 1 项")
        if len(value) > MAX_BATCH_FILES:
            raise ValueError(f"参数 {name} 不能超过 {MAX_BATCH_FILES} 项")

    @staticmethod
    def _validate_batch_item(
        item: Any,
        index: int,
        allowed: set[str],
        required: set[str],
    ) -> None:
        if not isinstance(item, dict):
            raise ValueError(f"批量操作第 {index + 1} 项必须是 JSON 对象")
        missing = required - set(item)
        if missing:
            raise ValueError(
                f"批量操作第 {index + 1} 项缺少参数: {', '.join(sorted(missing))}"
            )
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(
                f"批量操作第 {index + 1} 项包含未知参数: {', '.join(sorted(unknown))}"
            )

    def _commit_batch(self, updates: list[tuple[Path, str]]) -> None:
        originals: dict[Path, bytes | None] = {}
        rollback_bytes = 0
        for path, _content in updates:
            original = path.read_bytes() if path.exists() else None
            if original is not None:
                rollback_bytes += len(original)
                if rollback_bytes > MAX_BATCH_ROLLBACK_BYTES:
                    raise ValueError(
                        f"批量操作原文件总计不能超过 {MAX_BATCH_ROLLBACK_BYTES} 字节"
                    )
            originals[path] = original

        staged: dict[Path, Path] = {}
        committed: list[Path] = []
        try:
            for path, content in updates:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".coding-agent-",
                    suffix=".tmp",
                    dir=path.parent,
                )
                temporary = Path(temporary_name)
                staged[path] = temporary
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
            for path, _content in updates:
                os.replace(staged[path], path)
                staged.pop(path, None)
                committed.append(path)
        except Exception as exc:
            rollback_errors: list[str] = []
            for path in reversed(committed):
                try:
                    original = originals[path]
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(original)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{self._display_path(path)}: {rollback_exc}")
            detail = f"批量写入失败，已回滚: {exc}"
            if rollback_errors:
                detail += f"；回滚异常: {'; '.join(rollback_errors)}"
            raise OSError(detail) from exc
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)

    def _run_command(self, args: dict[str, Any]) -> ToolResult:
        command = args["command"]
        timeout = args.get("timeout_seconds", 60)
        decision = self.policy.classify(command)
        if decision.level == RiskLevel.DENY:
            return ToolResult(False, error=f"安全策略拒绝命令: {decision.reason}")
        needs_approval = self.approval_mode != "full" and decision.level == RiskLevel.REVIEW
        if self.approval_mode == "request" and not self.policy.is_read_only(command):
            needs_approval = True
        if needs_approval and not self.approver(command, decision.level, decision.reason):
            return ToolResult(False, error=f"用户未批准命令: {decision.reason}")

        if os.name == "nt":
            # PowerShell otherwise normalizes many native-process failures to 1.
            # Propagate the native exit code so the model can diagnose commands.
            wrapped = (
                f"{command}; "
                "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE } "
                "elseif (-not $?) { exit 1 }"
            )
            shell_command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapped]
        else:
            shell_command = ["/bin/sh", "-lc", command]
        return self._execute_process(shell_command, timeout)

    def _run_process(self, args: dict[str, Any]) -> ToolResult:
        raw_argv = args["argv"]
        timeout = args.get("timeout_seconds", 60)
        if (
            not isinstance(raw_argv, list)
            or not raw_argv
            or len(raw_argv) > 30
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 4_096
                or "\x00" in item
                or "\n" in item
                or "\r" in item
                for item in raw_argv
            )
        ):
            return ToolResult(False, error="argv 必须是 1 到 30 项的有界非空字符串数组")
        command = list(raw_argv)
        decision = self.policy.classify_argv(command)
        if decision.level == RiskLevel.DENY:
            return ToolResult(False, error=f"安全策略拒绝命令: {decision.reason}")
        needs_approval = self.approval_mode == "request" or (
            self.approval_mode != "full" and decision.level == RiskLevel.REVIEW
        )
        rendered = subprocess.list2cmdline(command)
        if needs_approval and not self.approver(rendered, decision.level, decision.reason):
            return ToolResult(False, error=f"用户未批准命令: {decision.reason}")
        return self._execute_process(command, timeout)

    def _execute_process(self, command: list[str], timeout: int) -> ToolResult:
        popen_options: dict[str, Any] = {
            "cwd": self.workspace,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(command, **popen_options)
        deadline = time.monotonic() + timeout
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                if self.is_cancelled() or time.monotonic() >= deadline:
                    self._terminate_process_tree(process)
                    try:
                        stdout, stderr = process.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        # A restricted Windows environment can prevent taskkill
                        # from closing an inherited child pipe immediately. Do
                        # not let output collection make the web task appear frozen.
                        process.kill()
                        if process.stdout:
                            process.stdout.close()
                        if process.stderr:
                            process.stderr.close()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass
                        stdout, stderr = "", ""
                    partial = "\n".join(part.rstrip() for part in [stdout, stderr] if part)
                    reason = "命令已由用户停止" if self.is_cancelled() else f"命令超过 {timeout} 秒后终止"
                    return ToolResult(False, output=self._truncate(partial), error=reason)
            except KeyboardInterrupt:
                self._terminate_process_tree(process)
                process.communicate()
                raise

        output = "\n".join(part for part in [stdout.rstrip(), stderr.rstrip()] if part)
        rendered = f"exit_code={process.returncode}"
        if output:
            rendered += f"\n{output}"
        return ToolResult(process.returncode == 0, self._truncate(rendered), None if process.returncode == 0 else "命令返回非零退出码")

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=1,
                )
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
