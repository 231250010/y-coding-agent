from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from coding_agent.devops_service import DevOpsOperationError, DevOpsService


class FakeDockerRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, float]] = []
        self.responses: list[subprocess.CompletedProcess[str]] = []

    def add(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.responses.append(
            subprocess.CompletedProcess(["docker"], returncode, stdout=stdout, stderr=stderr)
        )

    def __call__(
        self, command: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), cwd, timeout))
        if not self.responses:
            raise AssertionError(f"没有为命令配置响应: {command}")
        return self.responses.pop(0)


def write_compose(workspace: Path) -> Path:
    path = workspace / "compose.yaml"
    path.write_text("services:\n  web:\n    image: example/web\n", encoding="utf-8")
    return path


def test_inspect_detects_stack_compose_and_safe_default(tmp_path: Path) -> None:
    write_compose(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    result = DevOpsService(tmp_path).inspect()

    assert result["stacks"] == ["python"]
    assert result["compose_file"] == "compose.yaml"
    assert result["dockerfile"] is True
    assert result["default_environment"] == "local"
    assert result["environments"] == [{"name": "local", "docker_context": "current"}]
    assert result["ready"] is True


def test_preflight_uses_configured_remote_docker_context(tmp_path: Path) -> None:
    compose = write_compose(tmp_path)
    (tmp_path / "coding-agent.toml").write_text(
        """[devops]
compose_file = "compose.yaml"
default_environment = "staging"

[devops.environments.staging]
docker_context = "staging-host"
""",
        encoding="utf-8",
    )
    runner = FakeDockerRunner()
    runner.add("27.1.1\n")
    runner.add("2.29.1\n")
    runner.add()
    runner.add("web\nworker\n")

    result = DevOpsService(tmp_path, runner).preflight()

    prefix = ["docker", "--context", "staging-host"]
    assert runner.calls[0][0] == [*prefix, "version", "--format", "{{.Server.Version}}"]
    assert runner.calls[2][0] == [
        *prefix,
        "compose",
        "--file",
        str(compose),
        "config",
        "--quiet",
    ]
    assert result["environment"] == "staging"
    assert result["services"] == ["web", "worker"]
    assert result["valid"] is True


def test_config_cannot_escape_workspace(tmp_path: Path) -> None:
    (tmp_path / "coding-agent.toml").write_text(
        '[devops]\ncompose_file = "../compose.yaml"\n', encoding="utf-8"
    )

    with pytest.raises(DevOpsOperationError) as caught:
        DevOpsService(tmp_path).inspect()

    assert caught.value.code == "unsafe_operation"


def test_deploy_validates_then_verifies_healthy_services(tmp_path: Path) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    runner.add()
    runner.add("containers started")
    runner.add(
        json.dumps(
            [
                {"Service": "web", "State": "running", "Health": "healthy"},
                {"Service": "worker", "State": "running", "Health": ""},
            ]
        )
    )

    result = DevOpsService(tmp_path, runner).deploy(services=["web", "worker"])

    assert runner.calls[1][0][-5:] == ["up", "--detach", "--build", "web", "worker"]
    assert result["verification"]["healthy"] is True
    assert all(item["ready"] for item in result["verification"]["services"])


def test_verify_reports_unhealthy_or_stopped_service(tmp_path: Path) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    runner.add('{"Service":"web","State":"running","Health":"unhealthy"}\n')

    result = DevOpsService(tmp_path, runner).verify()

    assert result["healthy"] is False
    assert result["services"][0]["ready"] is False


def test_logs_are_bounded_validate_service_and_redact_secrets(tmp_path: Path) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    runner.add("token=super-secret\nhttps://user:pass@example.test/image")
    service = DevOpsService(tmp_path, runner)

    result = service.logs(service="web", tail=20)

    assert runner.calls[0][0][-5:] == ["logs", "--no-color", "--tail", "20", "web"]
    assert "super-secret" not in result["logs"]
    assert "user:pass" not in result["logs"]
    with pytest.raises(DevOpsOperationError, match="日志行数"):
        service.logs(tail=1001)
    with pytest.raises(DevOpsOperationError, match="服务名称"):
        service.logs(service="web; shutdown")


def test_docker_connection_failure_has_stable_error_code(tmp_path: Path) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    runner.add(stderr="Cannot connect to the Docker daemon", returncode=1)

    with pytest.raises(DevOpsOperationError) as caught:
        DevOpsService(tmp_path, runner).status()

    assert caught.value.code == "docker_unreachable"


def test_unparseable_status_is_not_reported_as_an_empty_environment(tmp_path: Path) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    runner.add("unexpected table output")

    with pytest.raises(DevOpsOperationError) as caught:
        DevOpsService(tmp_path, runner).status()

    assert caught.value.code == "invalid_response"
