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


def add_release_responses(
    runner: FakeDockerRunner,
    image_id: str,
    *,
    health: str = "healthy",
) -> None:
    runner.add()
    runner.add("service started")
    runner.add(
        json.dumps([{"Service": "web", "State": "running", "Health": health}])
    )
    runner.add(
        json.dumps(
            [
                {
                    "ContainerName": "demo-web-1",
                    "Repository": "demo-web",
                    "Tag": "latest",
                    "ID": image_id,
                }
            ]
        )
    )


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


def test_deploy_reports_real_phase_transitions(tmp_path: Path) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    runner.add()
    runner.add("started")
    runner.add('[{"Service":"web","State":"running","Health":"healthy"}]')
    events: list[dict[str, object]] = []

    DevOpsService(tmp_path, runner, on_progress=events.append).deploy()

    assert [(event["phase"], event["state"]) for event in events] == [
        ("validate", "running"),
        ("validate", "completed"),
        ("deploy", "running"),
        ("deploy", "completed"),
        ("verify", "running"),
        ("verify", "completed"),
    ]
    assert [event["percent"] for event in events] == [0, 33, 33, 67, 67, 100]


def test_running_docker_process_is_terminated_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_compose(tmp_path)

    class BlockingProcess:
        pid = 4242
        returncode: int | None = None
        stdout = None
        stderr = None
        killed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if not self.killed:
                raise subprocess.TimeoutExpired(["docker"], timeout or 0)
            return "partial build output", ""

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = BlockingProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    checks = iter([False, True])
    events: list[dict[str, object]] = []
    service = DevOpsService(
        tmp_path,
        is_cancelled=lambda: next(checks, True),
        on_progress=events.append,
    )
    monkeypatch.setattr(service, "_terminate_process_tree", lambda item: item.kill())

    with pytest.raises(DevOpsOperationError) as caught:
        service.status()

    assert caught.value.code == "operation_cancelled"
    assert caught.value.output == "partial build output"
    assert process.killed is True
    assert events[-1]["state"] == "cancelled"


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


def test_versioned_release_records_immutable_images_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_compose(workspace)
    runner = FakeDockerRunner()
    add_release_responses(runner, "sha256:111111111111")
    state_root = tmp_path / "agent-state" / "releases"
    service = DevOpsService(workspace, runner, release_state_root=state_root)

    result = service.release("v1.0.0", services=["web"])
    history = service.releases()

    assert result["status"] == "released"
    assert result["images"] == [
        {
            "id": "sha256:111111111111",
            "reference": "demo-web:latest",
            "container": "demo-web-1",
        }
    ]
    assert history["active_version"] == "v1.0.0"
    assert history["releases"][0]["version"] == "v1.0.0"
    assert list(state_root.glob("*.json"))
    assert not (workspace / ".coding-agent").exists()

    with pytest.raises(DevOpsOperationError) as caught:
        service.release("v1.0.0")
    assert caught.value.code == "release_exists"


def test_unhealthy_release_is_audited_but_not_activated(tmp_path: Path) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    add_release_responses(runner, "sha256:222222222222", health="unhealthy")
    service = DevOpsService(tmp_path, runner)

    with pytest.raises(DevOpsOperationError) as caught:
        service.release("v-bad")

    assert caught.value.code == "release_unhealthy"
    history = service.releases()
    assert history["active_version"] is None
    assert history["releases"][0]["status"] == "failed"


def test_rollback_requires_one_time_plan_restores_images_and_audits_result(
    tmp_path: Path,
) -> None:
    write_compose(tmp_path)
    runner = FakeDockerRunner()
    add_release_responses(runner, "sha256:111111111111")
    add_release_responses(runner, "sha256:222222222222")
    events: list[dict[str, object]] = []
    service = DevOpsService(tmp_path, runner, on_progress=events.append)
    service.release("v1")
    service.release("v2")

    plan = service.rollback_plan("v1")
    assert plan["from_version"] == "v2"
    assert plan["target_version"] == "v1"
    assert plan["requires_human_confirmation"] is True

    runner.add(
        '[{"ContainerName":"demo-web-1","Repository":"demo-web","Tag":"latest",'
        '"ID":"sha256:222222222222"}]'
    )
    runner.add("sha256:111111111111")
    runner.add()
    runner.add("recreated")
    runner.add('[{"Service":"web","State":"running","Health":"healthy"}]')

    result = service.rollback(plan["plan_id"])

    assert result["status"] == "rolled_back"
    assert result["active_version"] == "v1"
    assert any(
        call[0][-4:]
        == ["image", "tag", "sha256:111111111111", "demo-web:latest"]
        for call in runner.calls
    )
    history = service.releases()
    assert history["active_version"] == "v1"
    assert history["rollback_events"][0]["status"] == "rolled_back"
    assert any(event["operation"] == "compose_rollback" for event in events)

    with pytest.raises(DevOpsOperationError) as caught:
        service.rollback(plan["plan_id"])
    assert caught.value.code == "rollback_plan_used"
