from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DevOpsRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]
ProgressCallback = Callable[[dict[str, Any]], None]
CancelCallback = Callable[[], bool]
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle")


class DevOpsOperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.output = output


@dataclass(frozen=True, slots=True)
class DevOpsEnvironment:
    name: str
    docker_context: str | None = None


@dataclass(frozen=True, slots=True)
class DevOpsProjectConfig:
    compose_file: Path | None
    default_environment: str
    environments: dict[str, DevOpsEnvironment]


@dataclass(frozen=True, slots=True)
class ProgressStep:
    operation: str
    environment: str
    phase: str
    label: str
    current: int
    total: int


class DevOpsService:
    """Structured Docker Compose control plane for one developer workspace."""

    def __init__(
        self,
        workspace: Path,
        runner: DevOpsRunner | None = None,
        *,
        is_cancelled: CancelCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self._runner = runner
        self.is_cancelled = is_cancelled or (lambda: False)
        self.on_progress = on_progress or (lambda _data: None)

    def inspect(self) -> dict[str, Any]:
        config = self._load_config()
        manifests = [name for name in MANIFESTS if (self.workspace / name).is_file()]
        stacks: list[str] = []
        if "package.json" in manifests:
            stacks.append("node")
        if "pyproject.toml" in manifests:
            stacks.append("python")
        if "Cargo.toml" in manifests:
            stacks.append("rust")
        if "go.mod" in manifests:
            stacks.append("go")
        if "pom.xml" in manifests or "build.gradle" in manifests:
            stacks.append("java")
        return {
            "workspace": str(self.workspace),
            "stacks": stacks,
            "manifests": manifests,
            "dockerfile": (self.workspace / "Dockerfile").is_file(),
            "compose_file": self._relative(config.compose_file) if config.compose_file else None,
            "default_environment": config.default_environment,
            "environments": [
                {"name": item.name, "docker_context": item.docker_context or "current"}
                for item in config.environments.values()
            ],
            "ready": config.compose_file is not None,
        }

    def preflight(self, environment: str | None = None) -> dict[str, Any]:
        config, selected = self._resolve_environment(environment)
        compose_file = self._require_compose(config)
        docker = self._run(
            self._docker_command(selected, ["version", "--format", "{{.Server.Version}}"]),
            timeout=30,
            progress=self._step("compose_preflight", selected, "engine", "连接 Docker Engine", 1, 4),
        )
        compose = self._run(
            self._docker_command(selected, ["compose", "version", "--short"]),
            timeout=30,
            progress=self._step("compose_preflight", selected, "compose", "检查 Compose 版本", 2, 4),
        )
        self._run(
            self._compose_command(selected, compose_file, ["config", "--quiet"]),
            timeout=30,
            progress=self._step("compose_preflight", selected, "config", "校验 Compose 配置", 3, 4),
        )
        services = self._run(
            self._compose_command(selected, compose_file, ["config", "--services"]),
            timeout=30,
            progress=self._step("compose_preflight", selected, "services", "读取服务清单", 4, 4),
        ).stdout.splitlines()
        return {
            "environment": selected.name,
            "docker_context": selected.docker_context or "current",
            "docker_version": docker.stdout.strip(),
            "compose_version": compose.stdout.strip(),
            "compose_file": self._relative(compose_file),
            "services": [service.strip() for service in services if service.strip()],
            "valid": True,
        }

    def status(self, environment: str | None = None) -> dict[str, Any]:
        config, selected = self._resolve_environment(environment)
        compose_file = self._require_compose(config)
        services = self._status_records(
            selected,
            compose_file,
            self._step("compose_status", selected, "status", "读取服务状态", 1, 1),
        )
        return {
            "environment": selected.name,
            "services": services,
        }

    def logs(
        self, environment: str | None = None, service: str | None = None, tail: int = 200
    ) -> dict[str, Any]:
        if not 1 <= tail <= 1000:
            raise DevOpsOperationError("invalid_argument", "日志行数必须在 1 到 1000 之间")
        config, selected = self._resolve_environment(environment)
        compose_file = self._require_compose(config)
        args = ["logs", "--no-color", "--tail", str(tail)]
        if service:
            args.append(self._validate_service(service))
        result = self._run(
            self._compose_command(selected, compose_file, args),
            timeout=45,
            progress=self._step("compose_logs", selected, "logs", "读取服务日志", 1, 1),
        )
        return {
            "environment": selected.name,
            "service": service,
            "logs": self._combined_output(result),
        }

    def build(
        self, environment: str | None = None, services: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return self._write_operation("build", environment, services, timeout=600)

    def pull(
        self, environment: str | None = None, services: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return self._write_operation("pull", environment, services, timeout=600)

    def deploy(
        self, environment: str | None = None, services: Sequence[str] | None = None
    ) -> dict[str, Any]:
        config, selected = self._resolve_environment(environment)
        compose_file = self._require_compose(config)
        normalized = self._validate_services(services)
        self._run(
            self._compose_command(selected, compose_file, ["config", "--quiet"]),
            timeout=30,
            progress=self._step("compose_deploy", selected, "validate", "校验 Compose 配置", 1, 3),
        )
        result = self._run(
            self._compose_command(selected, compose_file, ["up", "--detach", "--build", *normalized]),
            timeout=600,
            progress=self._step("compose_deploy", selected, "deploy", "构建并启动服务", 2, 3),
        )
        verification = self._verify(
            selected,
            compose_file,
            self._step("compose_deploy", selected, "verify", "验证服务健康状态", 3, 3),
        )
        return {
            "environment": selected.name,
            "services": normalized,
            "output": self._combined_output(result),
            "verification": verification,
        }

    def verify(self, environment: str | None = None) -> dict[str, Any]:
        config, selected = self._resolve_environment(environment)
        compose_file = self._require_compose(config)
        return self._verify(
            selected,
            compose_file,
            self._step("compose_verify", selected, "verify", "验证服务健康状态", 1, 1),
        )

    def _verify(
        self,
        selected: DevOpsEnvironment,
        compose_file: Path,
        progress: ProgressStep,
    ) -> dict[str, Any]:
        services = self._status_records(selected, compose_file, progress)
        results: list[dict[str, Any]] = []
        for item in services:
            state = str(item.get("State") or item.get("state") or "").lower()
            health = str(item.get("Health") or item.get("health") or "").lower()
            results.append(
                {
                    "service": item.get("Service") or item.get("service") or item.get("Name") or "unknown",
                    "state": state or "unknown",
                    "health": health or "not-configured",
                    "ready": state == "running" and health in {"", "healthy"},
                }
            )
        return {
            "environment": selected.name,
            "healthy": bool(results) and all(item["ready"] for item in results),
            "services": results,
        }

    def restart(
        self, environment: str | None = None, services: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return self._write_operation("restart", environment, services, timeout=180)

    def stop(
        self, environment: str | None = None, services: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return self._write_operation("stop", environment, services, timeout=180)

    def _write_operation(
        self,
        operation: str,
        environment: str | None,
        services: Sequence[str] | None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        config, selected = self._resolve_environment(environment)
        compose_file = self._require_compose(config)
        normalized = self._validate_services(services)
        result = self._run(
            self._compose_command(selected, compose_file, [operation, *normalized]),
            timeout=timeout,
            progress=self._step(
                f"compose_{operation}",
                selected,
                operation,
                {
                    "build": "构建服务镜像",
                    "pull": "拉取服务镜像",
                    "restart": "重启服务",
                    "stop": "停止服务",
                }.get(operation, f"执行 {operation}"),
                1,
                1,
            ),
        )
        return {
            "operation": operation,
            "environment": selected.name,
            "services": normalized,
            "output": self._combined_output(result),
        }

    def _load_config(self) -> DevOpsProjectConfig:
        path = self.workspace / "coding-agent.toml"
        raw: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise DevOpsOperationError("config_invalid", f"coding-agent.toml 无法读取: {exc}") from exc
            section = loaded.get("devops", {})
            if not isinstance(section, dict):
                raise DevOpsOperationError("config_invalid", "[devops] 必须是 TOML 表")
            raw = section

        compose_value = raw.get("compose_file")
        compose_file: Path | None
        if compose_value is not None:
            if not isinstance(compose_value, str):
                raise DevOpsOperationError("config_invalid", "devops.compose_file 必须是字符串")
            compose_file = self._workspace_file(compose_value)
        else:
            compose_file = next(
                (self.workspace / name for name in COMPOSE_FILES if (self.workspace / name).is_file()),
                None,
            )

        environments: dict[str, DevOpsEnvironment] = {}
        raw_environments = raw.get("environments", {})
        if not isinstance(raw_environments, dict):
            raise DevOpsOperationError("config_invalid", "devops.environments 必须是 TOML 表")
        for name, value in raw_environments.items():
            if not isinstance(value, dict):
                raise DevOpsOperationError("config_invalid", f"环境 {name} 必须是 TOML 表")
            self._validate_name(name, "环境名称")
            context = value.get("docker_context")
            if context is not None:
                if not isinstance(context, str):
                    raise DevOpsOperationError("config_invalid", f"环境 {name} 的 docker_context 必须是字符串")
                self._validate_name(context, "Docker Context")
            environments[name] = DevOpsEnvironment(name, context)
        if not environments:
            environments["local"] = DevOpsEnvironment("local")

        default_environment = raw.get("default_environment", next(iter(environments)))
        if not isinstance(default_environment, str) or default_environment not in environments:
            raise DevOpsOperationError("config_invalid", "默认环境未在 devops.environments 中定义")
        return DevOpsProjectConfig(compose_file, default_environment, environments)

    def _resolve_environment(
        self, environment: str | None
    ) -> tuple[DevOpsProjectConfig, DevOpsEnvironment]:
        config = self._load_config()
        name = environment or config.default_environment
        selected = config.environments.get(name)
        if selected is None:
            raise DevOpsOperationError("environment_not_found", f"未配置 DevOps 环境: {name}")
        return config, selected

    def _require_compose(self, config: DevOpsProjectConfig) -> Path:
        if config.compose_file is None or not config.compose_file.is_file():
            raise DevOpsOperationError("compose_not_found", "项目中未找到 Docker Compose 配置")
        return config.compose_file

    def _workspace_file(self, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise DevOpsOperationError("unsafe_operation", f"配置路径超出工作区: {value}")
        resolved = (self.workspace / candidate).resolve(strict=False)
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise DevOpsOperationError("unsafe_operation", f"配置路径超出工作区: {value}") from exc
        return resolved

    @staticmethod
    def _validate_name(value: str, label: str) -> str:
        if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise DevOpsOperationError("invalid_argument", f"{label}无效: {value}")
        return value

    def _validate_service(self, value: str) -> str:
        return self._validate_name(value, "服务名称")

    def _validate_services(self, services: Sequence[str] | None) -> list[str]:
        if services is None:
            return []
        if isinstance(services, (str, bytes)) or len(services) > 100:
            raise DevOpsOperationError("invalid_argument", "services 必须是服务名称数组")
        return [self._validate_service(service) for service in services]

    @staticmethod
    def _docker_command(environment: DevOpsEnvironment, args: Sequence[str]) -> list[str]:
        command = ["docker"]
        if environment.docker_context:
            command.extend(["--context", environment.docker_context])
        return [*command, *args]

    def _compose_command(
        self, environment: DevOpsEnvironment, compose_file: Path, args: Sequence[str]
    ) -> list[str]:
        return self._docker_command(environment, ["compose", "--file", str(compose_file), *args])

    def _run(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        progress: ProgressStep | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._check_cancelled()
        started = time.monotonic()
        self._emit_progress(progress, "running", elapsed=0.0, completed=False)
        try:
            if self._runner is not None:
                result = self._runner(command, self.workspace, timeout)
                self._check_cancelled()
            else:
                result = self._run_process(command, timeout, progress, started)
        except FileNotFoundError as exc:
            self._emit_progress(progress, "failed", elapsed=time.monotonic() - started)
            raise DevOpsOperationError("docker_unavailable", "本机未找到 Docker CLI") from exc
        except subprocess.TimeoutExpired as exc:
            self._emit_progress(progress, "failed", elapsed=time.monotonic() - started)
            raise DevOpsOperationError("operation_timeout", "Docker Compose 操作超时") from exc
        except DevOpsOperationError as exc:
            state = "cancelled" if exc.code == "operation_cancelled" else "failed"
            self._emit_progress(progress, state, elapsed=time.monotonic() - started)
            raise
        except OSError as exc:
            self._emit_progress(progress, "failed", elapsed=time.monotonic() - started)
            raise DevOpsOperationError("compose_failed", f"无法启动 Docker CLI: {exc}") from exc
        if result.returncode != 0:
            self._emit_progress(progress, "failed", elapsed=time.monotonic() - started)
            self._raise_failure(result)
        self._emit_progress(
            progress,
            "completed",
            elapsed=time.monotonic() - started,
            completed=True,
        )
        return result

    def _run_process(
        self,
        command: Sequence[str],
        timeout: float,
        progress: ProgressStep | None,
        started: float,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["DOCKER_CLI_HINTS"] = "false"
        options: dict[str, Any] = {
            "cwd": self.workspace,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
            "env": environment,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(list(command), **options)
        last_tick = started
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                return subprocess.CompletedProcess(
                    list(command), process.returncode, stdout=stdout, stderr=stderr
                )
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if self.is_cancelled():
                    self._terminate_process_tree(process)
                    stdout, stderr = self._collect_after_termination(process)
                    partial = self._redact("\n".join(filter(None, (stdout.strip(), stderr.strip()))))
                    raise DevOpsOperationError(
                        "operation_cancelled",
                        "Docker Compose 操作已由用户停止",
                        output=partial,
                    )
                if now - started >= timeout:
                    self._terminate_process_tree(process)
                    self._collect_after_termination(process)
                    raise subprocess.TimeoutExpired(list(command), timeout)
                if now - last_tick >= 0.5:
                    self._emit_progress(progress, "running", elapsed=now - started, completed=False)
                    last_tick = now
            except KeyboardInterrupt:
                self._terminate_process_tree(process)
                self._collect_after_termination(process)
                raise

    def _status_records(
        self,
        selected: DevOpsEnvironment,
        compose_file: Path,
        progress: ProgressStep,
    ) -> list[dict[str, Any]]:
        result = self._run(
            self._compose_command(selected, compose_file, ["ps", "--format", "json"]),
            timeout=30,
            progress=progress,
        )
        services = self._parse_json_records(result.stdout)
        if result.stdout.strip() and not services:
            raise DevOpsOperationError(
                "invalid_response",
                "Docker Compose 返回了无法解析的服务状态",
                output=self._combined_output(result),
            )
        return services

    @staticmethod
    def _step(
        operation: str,
        environment: DevOpsEnvironment,
        phase: str,
        label: str,
        current: int,
        total: int,
    ) -> ProgressStep:
        return ProgressStep(operation, environment.name, phase, label, current, total)

    def _emit_progress(
        self,
        progress: ProgressStep | None,
        state: str,
        *,
        elapsed: float,
        completed: bool = False,
    ) -> None:
        if progress is None:
            return
        finished = progress.current if completed else progress.current - 1
        payload = {
            "operation": progress.operation,
            "environment": progress.environment,
            "phase": progress.phase,
            "label": progress.label,
            "current": progress.current,
            "total": progress.total,
            "percent": round(100 * finished / progress.total),
            "elapsed_seconds": round(max(0.0, elapsed), 1),
            "state": state,
        }
        try:
            self.on_progress(payload)
        except Exception:
            # Progress reporting is observational and must never break deployment.
            pass

    def _check_cancelled(self) -> None:
        if self.is_cancelled():
            raise DevOpsOperationError(
                "operation_cancelled", "Docker Compose 操作已由用户停止"
            )

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
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _collect_after_termination(process: subprocess.Popen[str]) -> tuple[str, str]:
        try:
            return process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            return "", ""

    def _raise_failure(self, result: subprocess.CompletedProcess[str]) -> None:
        output = self._combined_output(result)
        lowered = output.lower()
        if "cannot connect to the docker daemon" in lowered or "error during connect" in lowered:
            code, message = "docker_unreachable", "无法连接 Docker Engine"
        elif "validating" in lowered or "services." in lowered or "compose file" in lowered:
            code, message = "compose_invalid", "Docker Compose 配置无效"
        elif "no such service" in lowered:
            code, message = "service_not_found", "Docker Compose 服务不存在"
        else:
            code, message = "compose_failed", "Docker Compose 操作失败"
        raise DevOpsOperationError(code, message, output=output)

    @staticmethod
    def _parse_json_records(value: str) -> list[dict[str, Any]]:
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    @staticmethod
    def _redact(value: str) -> str:
        value = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1***@", value)
        return re.sub(
            r"(?i)((?:token|password|secret|authorization)\s*[=:]\s*)\S+",
            r"\1***",
            value,
        )

    @classmethod
    def _combined_output(cls, result: subprocess.CompletedProcess[str]) -> str:
        return cls._redact("\n".join(filter(None, (result.stdout.strip(), result.stderr.strip()))))
