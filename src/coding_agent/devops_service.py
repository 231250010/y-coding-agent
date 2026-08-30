from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import tomllib
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .release_store import ReleaseStore
from .safety import CommandPolicy, RiskLevel


DevOpsRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]
ProgressCallback = Callable[[dict[str, Any]], None]
CancelCallback = Callable[[], bool]
GateRunner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]
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
    health_timeout_seconds: float = 60.0
    health_interval_seconds: float = 2.0
    http_probes: tuple["HttpProbe", ...] = ()


@dataclass(frozen=True, slots=True)
class HttpProbe:
    name: str
    url: str
    expected_status: int = 200
    timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class ReleaseCheck:
    name: str
    command: tuple[str, ...]
    timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    require_git: bool = False
    require_clean_worktree: bool = False
    checks: tuple[ReleaseCheck, ...] = ()


@dataclass(frozen=True, slots=True)
class DevOpsProjectConfig:
    compose_file: Path | None
    default_environment: str
    environments: dict[str, DevOpsEnvironment]
    release_policy: ReleasePolicy


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

    _locks_guard = threading.Lock()
    _environment_locks: dict[str, threading.Lock] = {}
    _active_operations: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        workspace: Path,
        runner: DevOpsRunner | None = None,
        *,
        is_cancelled: CancelCallback | None = None,
        on_progress: ProgressCallback | None = None,
        release_state_root: Path | None = None,
        gate_runner: GateRunner | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self._runner = runner
        self.is_cancelled = is_cancelled or (lambda: False)
        self.on_progress = on_progress or (lambda _data: None)
        self.release_store = ReleaseStore(self.workspace, release_state_root)
        self._gate_runner = gate_runner

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
                {
                    "name": item.name,
                    "docker_context": item.docker_context or "current",
                    "health_timeout_seconds": item.health_timeout_seconds,
                    "http_probes": [probe.name for probe in item.http_probes],
                }
                for item in config.environments.values()
            ],
            "release_policy": {
                "require_git": config.release_policy.require_git,
                "require_clean_worktree": config.release_policy.require_clean_worktree,
                "checks": [check.name for check in config.release_policy.checks],
            },
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
        _config, selected = self._resolve_environment(environment)
        with self._environment_operation(selected, "compose_deploy"):
            return self._deploy_unlocked(environment, services)

    def _deploy_unlocked(
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
        started = time.monotonic()
        deadline = started + selected.health_timeout_seconds
        self._emit_progress(progress, "running", elapsed=0.0)
        attempts = 0
        results: list[dict[str, Any]] = []
        probe_results: list[dict[str, Any]] = []
        timed_out = False
        while True:
            self._check_cancelled()
            attempts += 1
            try:
                services = self._status_records(selected, compose_file, None)
                results = self._service_health(services)
                containers_ready = bool(results) and all(item["ready"] for item in results)
                terminal_failure = any(
                    item["state"] in {"dead", "exited", "removing"}
                    or item["health"] == "unhealthy"
                    for item in results
                )
                probe_results = (
                    self._run_http_probes(selected.http_probes) if containers_ready else []
                )
            except DevOpsOperationError as exc:
                self._emit_progress(
                    progress,
                    "cancelled" if exc.code == "operation_cancelled" else "failed",
                    elapsed=time.monotonic() - started,
                )
                raise
            probes_ready = all(item["ready"] for item in probe_results)
            if containers_ready and probes_ready:
                break
            now = time.monotonic()
            if terminal_failure or now >= deadline:
                timed_out = not terminal_failure and now >= deadline
                break
            self._emit_progress(progress, "running", elapsed=now - started)
            time.sleep(min(selected.health_interval_seconds, max(0.0, deadline - now)))

        healthy = bool(results) and all(item["ready"] for item in results) and all(
            item["ready"] for item in probe_results
        )
        self._emit_progress(
            progress,
            "completed",
            elapsed=time.monotonic() - started,
            completed=True,
        )
        return {
            "environment": selected.name,
            "healthy": healthy,
            "attempts": attempts,
            "timed_out": timed_out,
            "services": results,
            "http_probes": probe_results,
        }

    @staticmethod
    def _service_health(services: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
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
        return results

    def _run_http_probes(self, probes: Sequence[HttpProbe]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for probe in probes:
            self._check_cancelled()
            started = time.monotonic()
            status: int | None = None
            error = ""
            try:
                with urlrequest.urlopen(probe.url, timeout=probe.timeout_seconds) as response:
                    status = int(response.status)
            except urlerror.HTTPError as exc:
                status = int(exc.code)
                error = f"HTTP {exc.code}"
            except (OSError, ValueError, urlerror.URLError) as exc:
                error = str(exc)[:300]
            results.append(
                {
                    "name": probe.name,
                    "url": probe.url,
                    "expected_status": probe.expected_status,
                    "status": status,
                    "ready": status == probe.expected_status,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": error,
                }
            )
        return results

    def restart(
        self, environment: str | None = None, services: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return self._write_operation("restart", environment, services, timeout=180)

    def stop(
        self, environment: str | None = None, services: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return self._write_operation("stop", environment, services, timeout=180)

    def release(
        self,
        version: str,
        environment: str | None = None,
        services: Sequence[str] | None = None,
        *,
        expected_policy_digest: str | None = None,
        allow_review_checks: bool = False,
    ) -> dict[str, Any]:
        _config, selected = self._resolve_environment(environment)
        with self._environment_operation(selected, "compose_release"):
            return self._release_unlocked(
                version,
                environment,
                services,
                expected_policy_digest=expected_policy_digest,
                allow_review_checks=allow_review_checks,
            )

    def release_preview(self, environment: str | None = None) -> dict[str, Any]:
        config, selected = self._resolve_environment(environment)
        self._require_compose(config)
        policy = config.release_policy
        classifier = CommandPolicy(self.workspace)
        checks = []
        for check in policy.checks:
            decision = classifier.classify_argv(check.command)
            checks.append(
                {
                    "name": check.name,
                    "command": list(check.command),
                    "timeout_seconds": check.timeout_seconds,
                    "risk": decision.level.value,
                    "reason": decision.reason,
                }
            )
        return {
            "environment": selected.name,
            "require_git": policy.require_git,
            "require_clean_worktree": policy.require_clean_worktree,
            "checks": checks,
            "policy_digest": self._policy_digest(),
        }

    def _release_unlocked(
        self,
        version: str,
        environment: str | None = None,
        services: Sequence[str] | None = None,
        *,
        expected_policy_digest: str | None = None,
        allow_review_checks: bool = False,
    ) -> dict[str, Any]:
        release_version = self._validate_version(version)
        config, selected = self._resolve_environment(environment)
        compose_file = self._require_compose(config)
        normalized = self._validate_services(services)
        state = self._load_release_state()
        if self._find_release(state, selected.name, release_version) is not None:
            raise DevOpsOperationError(
                "release_exists",
                f"环境 {selected.name} 已存在版本 {release_version}",
            )

        gate: dict[str, Any] = {}
        self._local_progress_step(
            self._step("compose_release", selected, "gate", "执行发布门禁", 1, 6),
            lambda: gate.update(
                self._release_gate(
                    config,
                    compose_file,
                    expected_policy_digest=expected_policy_digest,
                    allow_review_checks=allow_review_checks,
                )
            ),
        )
        self._run(
            self._compose_command(selected, compose_file, ["config", "--quiet"]),
            timeout=30,
            progress=self._step("compose_release", selected, "validate", "校验发布配置", 2, 6),
        )
        deploy_result = self._run(
            self._compose_command(
                selected, compose_file, ["up", "--detach", "--build", *normalized]
            ),
            timeout=600,
            progress=self._step("compose_release", selected, "deploy", "构建并启动发布版本", 3, 6),
        )
        verification = self._verify(
            selected,
            compose_file,
            self._step("compose_release", selected, "verify", "验证发布健康状态", 4, 6),
        )
        images = self._capture_images(
            selected,
            compose_file,
            normalized,
            self._step("compose_release", selected, "inventory", "锁定不可变镜像标识", 5, 6),
        )
        if not images:
            raise DevOpsOperationError(
                "release_inventory_empty", "没有发现可记录的 Compose 服务镜像"
            )

        record = {
            "version": release_version,
            "environment": selected.name,
            "created_at": self._now(),
            "compose_file": self._relative(compose_file),
            "services": normalized,
            "images": images,
            "healthy": bool(verification["healthy"]),
            "status": "released" if verification["healthy"] else "failed",
            "provenance": gate,
        }
        self._local_progress_step(
            self._step("compose_release", selected, "record", "写入发布审计记录", 6, 6),
            lambda: self._record_release(state, record),
        )
        if not verification["healthy"]:
            raise DevOpsOperationError(
                "release_unhealthy",
                f"版本 {release_version} 已记录，但健康验证未通过，未设为活动版本",
                output=json.dumps(verification, ensure_ascii=False),
            )
        return {
            "version": release_version,
            "environment": selected.name,
            "status": "released",
            "images": images,
            "output": self._combined_output(deploy_result),
            "verification": verification,
            "provenance": gate,
        }

    def releases(self, environment: str | None = None, limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise DevOpsOperationError("invalid_argument", "发布记录数量必须在 1 到 100 之间")
        config, selected = self._resolve_environment(environment)
        self._require_compose(config)
        state = self._load_release_state()
        records = [
            item
            for item in state["releases"]
            if item.get("environment") == selected.name
        ]
        return {
            "environment": selected.name,
            "active_version": state["active"].get(selected.name),
            "releases": list(reversed(records[-limit:])),
            "rollback_events": list(
                reversed(
                    [
                        item
                        for item in state["rollback_events"]
                        if item.get("environment") == selected.name
                    ][-limit:]
                )
            ),
        }

    def rollback_plan(self, version: str, environment: str | None = None) -> dict[str, Any]:
        _config, selected = self._resolve_environment(environment)
        with self._environment_operation(selected, "compose_rollback_plan"):
            return self._rollback_plan_unlocked(version, environment)

    def _rollback_plan_unlocked(
        self, version: str, environment: str | None = None
    ) -> dict[str, Any]:
        release_version = self._validate_version(version)
        config, selected = self._resolve_environment(environment)
        self._require_compose(config)
        state = self._load_release_state()
        target = self._find_release(state, selected.name, release_version)
        if target is None or target.get("status") != "released":
            raise DevOpsOperationError(
                "release_not_found",
                f"环境 {selected.name} 没有可回滚的成功版本 {release_version}",
            )
        active = state["active"].get(selected.name)
        if active == release_version:
            raise DevOpsOperationError(
                "already_active", f"版本 {release_version} 已是环境 {selected.name} 的活动版本"
            )
        images = self._validated_release_images(target.get("images"))
        services = self._validate_services(target.get("services"))
        now = datetime.now(timezone.utc)
        plan = {
            "plan_id": uuid.uuid4().hex,
            "environment": selected.name,
            "from_version": active,
            "target_version": release_version,
            "services": services,
            "images": images,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "used_at": None,
        }
        state["rollback_plans"] = [
            item for item in state["rollback_plans"] if not self._plan_expired(item, now)
        ][-19:]
        state["rollback_plans"].append(plan)
        self._save_release_state(state)
        return self._plan_preview(plan)

    def rollback_preview(self, plan_id: str) -> dict[str, Any]:
        plan_key = self._validate_plan_id(plan_id)
        state = self._load_release_state()
        plan = self._find_plan(state, plan_key)
        self._validate_plan(plan)
        assert plan is not None
        return self._plan_preview(plan)

    def rollback(self, plan_id: str) -> dict[str, Any]:
        plan_key = self._validate_plan_id(plan_id)
        state = self._load_release_state()
        plan = self._find_plan(state, plan_key)
        self._validate_plan(plan)
        assert plan is not None
        _config, selected = self._resolve_environment(str(plan["environment"]))
        with self._environment_operation(selected, "compose_rollback"):
            return self._rollback_unlocked(plan_id)

    def _rollback_unlocked(self, plan_id: str) -> dict[str, Any]:
        plan_key = self._validate_plan_id(plan_id)
        state = self._load_release_state()
        plan = self._find_plan(state, plan_key)
        self._validate_plan(plan)
        assert plan is not None
        config, selected = self._resolve_environment(str(plan["environment"]))
        compose_file = self._require_compose(config)
        images = self._validated_release_images(plan.get("images"))
        services = self._validate_services(plan.get("services"))

        self._local_progress_step(
            self._step("compose_rollback", selected, "plan", "锁定一次性回滚计划", 1, 5),
            lambda: self._mark_plan_used(state, plan),
        )
        event = {
            "plan_id": plan_key,
            "environment": selected.name,
            "from_version": plan.get("from_version"),
            "target_version": plan["target_version"],
            "started_at": self._now(),
            "finished_at": None,
            "status": "running",
            "before_images": [],
        }
        state["rollback_events"].append(event)
        self._save_release_state(state)
        try:
            event["before_images"] = self._capture_images(
                selected,
                compose_file,
                services,
                self._step("compose_rollback", selected, "snapshot", "记录当前镜像现场", 2, 5),
            )
            self._save_release_state(state)
            total_tags = len(images)
            image_progress = self._step(
                "compose_rollback", selected, "images", "恢复目标版本镜像标签", 3, 5
            )
            image_started = time.monotonic()
            self._emit_progress(image_progress, "running", elapsed=0.0)
            try:
                for index, image in enumerate(images, start=1):
                    image_progress = self._step(
                        "compose_rollback",
                        selected,
                        "images",
                        f"恢复目标镜像 {index}/{total_tags}",
                        3,
                        5,
                    )
                    self._emit_progress(
                        image_progress,
                        "running",
                        elapsed=time.monotonic() - image_started,
                    )
                    self._run(
                        self._docker_command(selected, ["image", "inspect", image["id"]]),
                        timeout=30,
                    )
                    self._run(
                        self._docker_command(
                            selected, ["image", "tag", image["id"], image["reference"]]
                        ),
                        timeout=30,
                    )
            except DevOpsOperationError:
                self._emit_progress(
                    image_progress,
                    "failed",
                    elapsed=time.monotonic() - image_started,
                )
                raise
            self._emit_progress(
                image_progress,
                "completed",
                elapsed=time.monotonic() - image_started,
                completed=True,
            )
            result = self._run(
                self._compose_command(
                    selected,
                    compose_file,
                    ["up", "--detach", "--no-build", *services],
                ),
                timeout=300,
                progress=self._step("compose_rollback", selected, "recreate", "按目标版本重建服务", 4, 5),
            )
            verification = self._verify(
                selected,
                compose_file,
                self._step("compose_rollback", selected, "verify", "验证回滚后的健康状态", 5, 5),
            )
            event["status"] = "rolled_back" if verification["healthy"] else "unhealthy"
            event["finished_at"] = self._now()
            event["verification"] = verification
            if verification["healthy"]:
                state["active"][selected.name] = plan["target_version"]
            self._save_release_state(state)
            if not verification["healthy"]:
                raise DevOpsOperationError(
                    "rollback_unhealthy",
                    "回滚命令已执行，但健康验证未通过",
                    output=json.dumps(verification, ensure_ascii=False),
                )
            return {
                "environment": selected.name,
                "from_version": plan.get("from_version"),
                "active_version": plan["target_version"],
                "status": "rolled_back",
                "output": self._combined_output(result),
                "verification": verification,
            }
        except DevOpsOperationError as exc:
            if event["status"] == "running":
                event["status"] = "cancelled" if exc.code == "operation_cancelled" else "failed"
                event["finished_at"] = self._now()
                event["error_code"] = exc.code
                self._save_release_state(state)
            raise

    def _release_gate(
        self,
        config: DevOpsProjectConfig,
        compose_file: Path,
        *,
        expected_policy_digest: str | None,
        allow_review_checks: bool,
    ) -> dict[str, Any]:
        policy = config.release_policy
        current_digest = self._policy_digest()
        classifier = CommandPolicy(self.workspace)
        decisions = [classifier.classify_argv(check.command) for check in policy.checks]
        if (
            expected_policy_digest is not None
            and expected_policy_digest != current_digest
        ):
            raise DevOpsOperationError(
                "release_gate_approval_required",
                "发布确认后 coding-agent.toml 已经变化，请重新预览并确认",
            )
        denied = [
            f"{check.name}: {decision.reason}"
            for check, decision in zip(policy.checks, decisions)
            if decision.level is RiskLevel.DENY
        ]
        if denied:
            raise DevOpsOperationError(
                "release_gate_denied", "发布门禁包含禁止命令: " + "；".join(denied)
            )
        review = [
            f"{check.name}: {decision.reason}"
            for check, decision in zip(policy.checks, decisions)
            if decision.level is RiskLevel.REVIEW
        ]
        if review and (not allow_review_checks or expected_policy_digest is None):
            raise DevOpsOperationError(
                "release_gate_approval_required",
                "发布门禁包含需要人工确认的命令，或确认后配置已经变化",
                output=json.dumps(review, ensure_ascii=False),
            )
        evidence: dict[str, Any] = {
            "evaluated_at": self._now(),
            "compose_sha256": hashlib.sha256(compose_file.read_bytes()).hexdigest(),
            "git": {"available": False, "commit": None, "branch": None, "dirty": None},
            "checks": [],
            "passed": False,
            "policy_digest": current_digest,
        }
        failures: list[str] = []
        root = self._run_gate_command(["git", "rev-parse", "--show-toplevel"], 15)
        if root.returncode == 0:
            commit = self._run_gate_command(["git", "rev-parse", "HEAD"], 15)
            branch = self._run_gate_command(["git", "branch", "--show-current"], 15)
            status = self._run_gate_command(["git", "status", "--porcelain"], 30)
            git_ok = commit.returncode == 0 and status.returncode == 0
            dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
            evidence["git"] = {
                "available": git_ok,
                "root": root.stdout.strip(),
                "commit": commit.stdout.strip() or None,
                "branch": branch.stdout.strip() or None,
                "dirty": dirty,
            }
        git_evidence = evidence["git"]
        if (policy.require_git or policy.require_clean_worktree) and not git_evidence["available"]:
            failures.append("当前工作区没有可验证的 Git 提交")
        if policy.require_clean_worktree and git_evidence["dirty"] is True:
            failures.append("Git 工作区存在未提交变更")

        for check in policy.checks:
            started = time.monotonic()
            result = self._run_gate_command(check.command, check.timeout_seconds)
            item = {
                "name": check.name,
                "command": list(check.command),
                "passed": result.returncode == 0,
                "returncode": result.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "output": self._redact(
                    "\n".join(filter(None, (result.stdout.strip(), result.stderr.strip())))
                )[:2000],
            }
            evidence["checks"].append(item)
            if not item["passed"]:
                failures.append(f"发布检查未通过: {check.name}")

        evidence["passed"] = not failures
        evidence["failures"] = failures
        if failures:
            raise DevOpsOperationError(
                "release_gate_failed",
                "发布门禁未通过: " + "；".join(failures),
                output=json.dumps(evidence, ensure_ascii=False, indent=2),
            )
        return evidence

    def _policy_digest(self) -> str:
        path = self.workspace / "coding-agent.toml"
        payload = path.read_bytes() if path.is_file() else b"<missing>"
        return hashlib.sha256(payload).hexdigest()

    def _run_gate_command(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self._check_cancelled()
        try:
            if self._gate_runner is not None:
                result = self._gate_runner(command, self.workspace, timeout)
            else:
                result = subprocess.run(
                    list(command),
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    timeout=timeout,
                    check=False,
                )
        except FileNotFoundError:
            return subprocess.CompletedProcess(list(command), 127, "", "command not found")
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "command timed out")
            return subprocess.CompletedProcess(
                list(command), 124, stdout, stderr
            )
        self._check_cancelled()
        return result

    def _capture_images(
        self,
        selected: DevOpsEnvironment,
        compose_file: Path,
        services: Sequence[str],
        progress: ProgressStep,
    ) -> list[dict[str, str]]:
        result = self._run(
            self._compose_command(
                selected, compose_file, ["images", "--format", "json", *services]
            ),
            timeout=60,
            progress=progress,
        )
        images: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in self._parse_json_records(result.stdout):
            image_id = str(item.get("ID") or item.get("Id") or item.get("ImageID") or "")
            repository = str(item.get("Repository") or "")
            tag = str(item.get("Tag") or "")
            if not image_id or not repository or repository == "<none>" or not tag or tag == "<none>":
                continue
            self._validate_image_id(image_id)
            reference = self._validate_image_reference(f"{repository}:{tag}")
            key = (image_id, reference)
            if key in seen:
                continue
            seen.add(key)
            images.append(
                {
                    "id": image_id,
                    "reference": reference,
                    "container": str(
                        item.get("ContainerName") or item.get("Container") or ""
                    )[:200],
                }
            )
        return images

    def _record_release(self, state: dict[str, Any], record: dict[str, Any]) -> None:
        state["releases"].append(record)
        if record["healthy"]:
            state["active"][record["environment"]] = record["version"]
        self._save_release_state(state)

    def _mark_plan_used(self, state: dict[str, Any], plan: dict[str, Any]) -> None:
        plan["used_at"] = self._now()
        self._save_release_state(state)

    def _local_progress_step(
        self, progress: ProgressStep, action: Callable[[], None]
    ) -> None:
        self._check_cancelled()
        started = time.monotonic()
        self._emit_progress(progress, "running", elapsed=0.0)
        try:
            action()
        except DevOpsOperationError:
            self._emit_progress(progress, "failed", elapsed=time.monotonic() - started)
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            self._emit_progress(progress, "failed", elapsed=time.monotonic() - started)
            raise DevOpsOperationError(
                "release_store_failed", f"发布审计记录写入失败: {exc}"
            ) from exc
        self._emit_progress(
            progress,
            "completed",
            elapsed=time.monotonic() - started,
            completed=True,
        )

    def _load_release_state(self) -> dict[str, Any]:
        try:
            return self.release_store.load()
        except (OSError, UnicodeError, ValueError) as exc:
            raise DevOpsOperationError(
                "release_store_failed", f"发布审计记录读取失败: {exc}"
            ) from exc

    def _save_release_state(self, state: dict[str, Any]) -> None:
        try:
            self.release_store.save(state)
        except (OSError, UnicodeError, ValueError) as exc:
            raise DevOpsOperationError(
                "release_store_failed", f"发布审计记录写入失败: {exc}"
            ) from exc

    @staticmethod
    def _find_release(
        state: dict[str, Any], environment: str, version: str
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in state["releases"]
                if item.get("environment") == environment and item.get("version") == version
            ),
            None,
        )

    @staticmethod
    def _find_plan(state: dict[str, Any], plan_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in state["rollback_plans"] if item.get("plan_id") == plan_id),
            None,
        )

    def _validate_plan(self, plan: dict[str, Any] | None) -> None:
        if plan is None:
            raise DevOpsOperationError("rollback_plan_not_found", "回滚计划不存在")
        if plan.get("used_at"):
            raise DevOpsOperationError("rollback_plan_used", "回滚计划已经执行，不能重复使用")
        if self._plan_expired(plan, datetime.now(timezone.utc)):
            raise DevOpsOperationError("rollback_plan_expired", "回滚计划已过期，请重新预览")

    @staticmethod
    def _plan_expired(plan: dict[str, Any], now: datetime) -> bool:
        expires_at = plan.get("expires_at")
        if not isinstance(expires_at, str):
            return True
        try:
            expiry = datetime.fromisoformat(expires_at)
        except ValueError:
            return True
        return expiry.tzinfo is None or expiry <= now

    @staticmethod
    def _plan_preview(plan: dict[str, Any]) -> dict[str, Any]:
        images = plan.get("images") if isinstance(plan.get("images"), list) else []
        return {
            "plan_id": plan["plan_id"],
            "environment": plan["environment"],
            "from_version": plan.get("from_version"),
            "target_version": plan["target_version"],
            "services": list(plan.get("services") or []),
            "images": [
                {
                    "reference": item.get("reference"),
                    "image_id": str(item.get("id") or "")[:20],
                }
                for item in images
                if isinstance(item, dict)
            ],
            "expires_at": plan["expires_at"],
            "requires_human_confirmation": True,
            "warning": "回滚会重新标记镜像并重建服务；不会删除数据卷，也不会自动回滚数据库。",
        }

    def _validated_release_images(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or not value:
            raise DevOpsOperationError("release_inventory_empty", "目标版本没有镜像记录")
        images: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                raise DevOpsOperationError("release_inventory_invalid", "目标版本镜像记录无效")
            image_id = self._validate_image_id(str(item.get("id") or ""))
            reference = self._validate_image_reference(str(item.get("reference") or ""))
            images.append({"id": image_id, "reference": reference})
        return images

    @staticmethod
    def _validate_version(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
            raise DevOpsOperationError(
                "invalid_argument", "版本号只能包含字母、数字、点、下划线和连字符"
            )
        return value

    @staticmethod
    def _validate_plan_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise DevOpsOperationError("invalid_argument", "回滚计划 ID 无效")
        return value

    @staticmethod
    def _validate_image_id(value: str) -> str:
        if not value or not re.fullmatch(r"[A-Za-z0-9:_.-]{6,200}", value):
            raise DevOpsOperationError("release_inventory_invalid", "镜像 ID 无效")
        return value

    @staticmethod
    def _validate_image_reference(value: str) -> str:
        if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}", value):
            raise DevOpsOperationError("release_inventory_invalid", "镜像引用无效")
        return value

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _environment_operation(
        self, selected: DevOpsEnvironment, operation: str
    ) -> Any:
        key = "|".join(
            (
                str(self.workspace).casefold(),
                (selected.docker_context or "current").casefold(),
                selected.name.casefold(),
            )
        )
        with self._locks_guard:
            lock = self._environment_locks.setdefault(key, threading.Lock())
            active = self._active_operations.get(key)
            if not lock.acquire(blocking=False):
                detail = json.dumps(active or {}, ensure_ascii=False)
                raise DevOpsOperationError(
                    "environment_busy",
                    f"环境 {selected.name} 正在执行其他变更操作，请等待完成或取消后重试",
                    output=detail,
                )
            self._active_operations[key] = {
                "operation": operation,
                "environment": selected.name,
                "started_at": self._now(),
            }
        try:
            yield
        finally:
            with self._locks_guard:
                self._active_operations.pop(key, None)
                lock.release()

    def _write_operation(
        self,
        operation: str,
        environment: str | None,
        services: Sequence[str] | None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        _config, selected = self._resolve_environment(environment)
        with self._environment_operation(selected, f"compose_{operation}"):
            return self._write_operation_unlocked(
                operation, environment, services, timeout=timeout
            )

    def _write_operation_unlocked(
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
            health_timeout = self._config_number(
                value.get("health_timeout_seconds", 60),
                f"环境 {name} 的 health_timeout_seconds",
                minimum=0,
                maximum=600,
            )
            health_interval = self._config_number(
                value.get("health_interval_seconds", 2),
                f"环境 {name} 的 health_interval_seconds",
                minimum=0.1,
                maximum=30,
            )
            probes = self._parse_http_probes(name, value.get("http_probes", []))
            environments[name] = DevOpsEnvironment(
                name, context, health_timeout, health_interval, probes
            )
        if not environments:
            environments["local"] = DevOpsEnvironment("local")

        default_environment = raw.get("default_environment", next(iter(environments)))
        if not isinstance(default_environment, str) or default_environment not in environments:
            raise DevOpsOperationError("config_invalid", "默认环境未在 devops.environments 中定义")
        release_policy = self._parse_release_policy(raw.get("release", {}))
        return DevOpsProjectConfig(
            compose_file, default_environment, environments, release_policy
        )

    def _parse_http_probes(self, environment: str, value: Any) -> tuple[HttpProbe, ...]:
        if not isinstance(value, list) or len(value) > 20:
            raise DevOpsOperationError(
                "config_invalid", f"环境 {environment} 的 http_probes 必须是有界数组"
            )
        probes: list[HttpProbe] = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise DevOpsOperationError("config_invalid", "http_probes 每一项必须是 TOML 表")
            name = item.get("name", f"probe-{index}")
            url = item.get("url")
            if not isinstance(name, str) or not name or len(name) > 80:
                raise DevOpsOperationError("config_invalid", "HTTP 探针名称无效")
            if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s]{1,500}", url):
                raise DevOpsOperationError("config_invalid", f"HTTP 探针 {name} 的 URL 无效")
            status = item.get("expected_status", 200)
            if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                raise DevOpsOperationError("config_invalid", f"HTTP 探针 {name} 的状态码无效")
            timeout = self._config_number(
                item.get("timeout_seconds", 3),
                f"HTTP 探针 {name} 的 timeout_seconds",
                minimum=0.1,
                maximum=30,
            )
            probes.append(HttpProbe(name, url, status, timeout))
        return tuple(probes)

    def _parse_release_policy(self, value: Any) -> ReleasePolicy:
        if not isinstance(value, dict):
            raise DevOpsOperationError("config_invalid", "devops.release 必须是 TOML 表")
        require_git = self._config_bool(value.get("require_git", False), "release.require_git")
        require_clean = self._config_bool(
            value.get("require_clean_worktree", False),
            "release.require_clean_worktree",
        )
        raw_checks = value.get("checks", [])
        if not isinstance(raw_checks, list) or len(raw_checks) > 20:
            raise DevOpsOperationError("config_invalid", "release.checks 必须是有界数组")
        checks: list[ReleaseCheck] = []
        for index, item in enumerate(raw_checks, start=1):
            if not isinstance(item, dict):
                raise DevOpsOperationError("config_invalid", "release.checks 每一项必须是 TOML 表")
            name = item.get("name", f"check-{index}")
            command = item.get("command")
            if not isinstance(name, str) or not name or len(name) > 80:
                raise DevOpsOperationError("config_invalid", "发布检查名称无效")
            if (
                not isinstance(command, list)
                or not command
                or len(command) > 30
                or any(not isinstance(part, str) or not part or len(part) > 300 for part in command)
            ):
                raise DevOpsOperationError(
                    "config_invalid", f"发布检查 {name} 的 command 必须是非空字符串数组"
                )
            timeout = self._config_number(
                item.get("timeout_seconds", 300),
                f"发布检查 {name} 的 timeout_seconds",
                minimum=1,
                maximum=1800,
            )
            checks.append(ReleaseCheck(name, tuple(command), timeout))
        return ReleasePolicy(require_git, require_clean, tuple(checks))

    @staticmethod
    def _config_bool(value: Any, label: str) -> bool:
        if not isinstance(value, bool):
            raise DevOpsOperationError("config_invalid", f"{label} 必须是布尔值")
        return value

    @staticmethod
    def _config_number(
        value: Any, label: str, *, minimum: float, maximum: float
    ) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not minimum <= float(value) <= maximum
        ):
            raise DevOpsOperationError(
                "config_invalid", f"{label} 必须在 {minimum} 到 {maximum} 之间"
            )
        return float(value)

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
