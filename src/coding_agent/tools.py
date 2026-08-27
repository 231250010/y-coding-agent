from __future__ import annotations

import fnmatch
import json
import os
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .changes import Capture, ChangeSet, ConversationChangeTracker
from .safety import CommandPolicy, RiskLevel


MAX_FILE_CHARS = 200_000
MAX_TOOL_OUTPUT = 16_000
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
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def resolve(self, user_path: str, *, must_exist: bool = False) -> Path:
        candidate_input = Path(user_path)
        candidate = candidate_input if candidate_input.is_absolute() else self.workspace / candidate_input
        resolved = candidate.resolve(strict=False)
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
    def __init__(
        self,
        workspace: Path | None,
        *,
        approver: ApprovalCallback | None = None,
        is_cancelled: CancelCallback | None = None,
        approval_mode: str = "ask",
        max_output: int = MAX_TOOL_OUTPUT,
        change_tracker: ConversationChangeTracker | None = None,
    ) -> None:
        self.approver = approver or (lambda _command, _risk, _reason: False)
        self.is_cancelled = is_cancelled or (lambda: False)
        self.approval_mode = approval_mode
        self.max_output = max_output
        self.change_tracker = change_tracker
        if workspace is None:
            self.workspace = None
            self.guard = None
            self.policy = None
            self._tools = {}
            return
        self.workspace = workspace.resolve()
        self.guard = PathGuard(self.workspace)
        self.policy = CommandPolicy(self.workspace)
        self._tools = {tool.name: tool for tool in self._build_tools()}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if self.workspace is None:
            return ToolResult(False, error="当前对话尚未选择工作目录")
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, error=f"未知工具: {name}")
        validation_error = self._validate(arguments, tool.parameters)
        if validation_error:
            return ToolResult(False, error=validation_error)

        capture: Capture | None = None
        try:
            if self.change_tracker and name in {"write_file", "replace_text"}:
                capture = self.change_tracker.capture_paths([str(arguments["path"])])
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
        python_types = {"string": str, "integer": int, "boolean": bool}
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
        return None

    def _build_tools(self) -> list[LocalTool]:
        common_path = {"type": "string", "description": "相对于工作区的路径"}
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
            results.append(f"{item.relative_to(self.workspace).as_posix()}{kind}")
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
                        matches.append(f"{path.relative_to(self.workspace).as_posix()}:{number}:{line}")
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
        return ToolResult(True, f"已写入 {path.relative_to(self.workspace).as_posix()}（{len(content)} 个字符）")

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
        return ToolResult(True, f"已在 {path.relative_to(self.workspace).as_posix()} 中替换 {replacements} 处")

    def _run_command(self, args: dict[str, Any]) -> ToolResult:
        command = args["command"]
        timeout = args.get("timeout_seconds", 60)
        decision = self.policy.classify(command)
        if decision.level == RiskLevel.DENY:
            return ToolResult(False, error=f"安全策略拒绝命令: {decision.reason}")
        needs_approval = self.approval_mode == "always" or decision.level == RiskLevel.REVIEW
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
        process = subprocess.Popen(shell_command, **popen_options)
        deadline = time.monotonic() + timeout
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(shell_command, timeout)
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
                        # not let output collection make the GUI appear frozen.
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
