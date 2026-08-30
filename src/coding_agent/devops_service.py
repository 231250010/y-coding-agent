from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DevOpsRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]
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


class DevOpsService:
    """Structured Docker Compose control plane for one developer workspace."""

    def __init__(self, workspace: Path, runner: DevOpsRunner | None = None) -> None:
        self.workspace = workspace.resolve()
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(
        command: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["DOCKER_CLI_HINTS"] = "false"
        return subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout,
            env=environment,
            check=False,
        )

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
        docker = self._run(self._docker_command(selected, ["version", "--format", "{{.Server.Version}}"]), timeout=30)
        compose = self._run(self._docker_command(selected, ["compose", "version", "--short"]), timeout=30)
        self._run(self._compose_command(selected, compose_file, ["config", "--quiet"]), timeout=30)
        services = self._run(
            self._compose_command(selected, compose_file, ["config", "--services"]), timeout=30
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
        result = self._run(
            self._compose_command(selected, compose_file, ["ps", "--format", "json"]), timeout=30
        )
        services = self._parse_json_records(result.stdout)
        if result.stdout.strip() and not services:
            raise DevOpsOperationError(
                "invalid_response",
                "Docker Compose 返回了无法解析的服务状态",
                output=self._combined_output(result),
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
        result = self._run(self._compose_command(selected, compose_file, args), timeout=45)
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
        self._run(self._compose_command(selected, compose_file, ["config", "--quiet"]), timeout=30)
        result = self._run(
            self._compose_command(selected, compose_file, ["up", "--detach", "--build", *normalized]),
            timeout=600,
        )
        verification = self.verify(selected.name)
        return {
            "environment": selected.name,
            "services": normalized,
            "output": self._combined_output(result),
            "verification": verification,
        }

    def verify(self, environment: str | None = None) -> dict[str, Any]:
        status = self.status(environment)
        services = status["services"]
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
            "environment": status["environment"],
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
            self._compose_command(selected, compose_file, [operation, *normalized]), timeout=timeout
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
        self, command: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(command, self.workspace, timeout)
        except FileNotFoundError as exc:
            raise DevOpsOperationError("docker_unavailable", "本机未找到 Docker CLI") from exc
        except subprocess.TimeoutExpired as exc:
            raise DevOpsOperationError("operation_timeout", "Docker Compose 操作超时") from exc
        except OSError as exc:
            raise DevOpsOperationError("compose_failed", f"无法启动 Docker CLI: {exc}") from exc
        if result.returncode != 0:
            self._raise_failure(result)
        return result

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
