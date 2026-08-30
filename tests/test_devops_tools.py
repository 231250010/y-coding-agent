from __future__ import annotations

import json
from typing import Any

from coding_agent.devops_service import DevOpsOperationError
from coding_agent.devops_tools import DevOpsToolProvider
from coding_agent.safety import RiskLevel


class StubDevOpsService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.failure: DevOpsOperationError | None = None
        self.release_preview_data: dict[str, Any] = {
            "environment": "local",
            "require_git": True,
            "require_clean_worktree": True,
            "checks": [],
            "policy_digest": "d" * 64,
        }

    def _call(self, name: str, *args: Any) -> dict[str, Any]:
        self.calls.append((name, args))
        if self.failure:
            raise self.failure
        return {"operation": name, "args": list(args)}

    def inspect(self) -> dict[str, Any]:
        return self._call("inspect")

    def preflight(self, environment: str | None = None) -> dict[str, Any]:
        return self._call("preflight", environment)

    def status(self, environment: str | None = None) -> dict[str, Any]:
        return self._call("status", environment)

    def logs(self, environment: str | None, service: str | None, tail: int) -> dict[str, Any]:
        return self._call("logs", environment, service, tail)

    def build(self, environment: str | None, services: list[str] | None) -> dict[str, Any]:
        return self._call("build", environment, services)

    pull = build
    deploy = build
    restart = build
    stop = build

    def release(
        self,
        version: str,
        environment: str | None,
        services: list[str] | None,
        *,
        expected_policy_digest: str | None = None,
        allow_review_checks: bool = False,
    ) -> dict[str, Any]:
        return self._call(
            "release",
            version,
            environment,
            services,
            expected_policy_digest,
            allow_review_checks,
        )

    def release_preview(self, environment: str | None) -> dict[str, Any]:
        preview = dict(self.release_preview_data)
        preview["environment"] = environment or preview["environment"]
        return preview

    def releases(self, environment: str | None, limit: int) -> dict[str, Any]:
        return self._call("releases", environment, limit)

    def rollback_plan(self, version: str, environment: str | None) -> dict[str, Any]:
        return self._call("rollback_plan", version, environment)

    def rollback_preview(self, plan_id: str) -> dict[str, Any]:
        self.calls.append(("rollback_preview", (plan_id,)))
        if self.failure:
            raise self.failure
        return {
            "plan_id": plan_id,
            "environment": "production",
            "from_version": "v2",
            "target_version": "v1",
        }

    def rollback(self, plan_id: str) -> dict[str, Any]:
        return self._call("rollback", plan_id)

    def verify(self, environment: str | None = None) -> dict[str, Any]:
        return self._call("verify", environment)


def tool_names(provider: DevOpsToolProvider) -> list[str]:
    return [item["function"]["name"] for item in provider.schemas()]


def test_exposes_complete_compose_lifecycle() -> None:
    provider = DevOpsToolProvider(StubDevOpsService())  # type: ignore[arg-type]

    assert tool_names(provider) == [
        "devops_inspect",
        "compose_preflight",
        "compose_status",
        "compose_logs",
        "compose_build",
        "compose_pull",
        "compose_deploy",
        "compose_release",
        "compose_releases",
        "compose_rollback_plan",
        "compose_rollback",
        "compose_verify",
        "compose_restart",
        "compose_stop",
    ]


def test_read_operations_do_not_request_approval() -> None:
    approvals: list[tuple[str, RiskLevel, str]] = []
    service = StubDevOpsService()
    provider = DevOpsToolProvider(
        service,  # type: ignore[arg-type]
        approval_mode="request",
        approver=lambda *args: approvals.append(args) or False,
    )

    result = provider.execute("compose_status", {"environment": "staging"})

    assert result.ok is True
    assert approvals == []
    assert service.calls == [("status", ("staging",))]


def test_mutations_require_approval_in_request_and_risk_modes() -> None:
    for mode in ("request", "risk"):
        service = StubDevOpsService()
        provider = DevOpsToolProvider(
            service,  # type: ignore[arg-type]
            approval_mode=mode,
            approver=lambda _command, risk, _reason: risk is RiskLevel.REVIEW and False,
        )

        result = provider.execute("compose_deploy", {"environment": "production"})

        assert result.ok is False
        assert "未批准" in (result.error or "")
        assert service.calls == []


def test_full_mode_executes_mutation_and_returns_structured_json() -> None:
    service = StubDevOpsService()
    provider = DevOpsToolProvider(service, approval_mode="full")  # type: ignore[arg-type]

    result = provider.execute("compose_deploy", {"services": ["web"]})

    assert result.ok is True
    assert json.loads(result.output)["operation"] == "build"
    assert service.calls == [("build", (None, ["web"]))]


def test_release_approval_displays_all_gate_commands() -> None:
    service = StubDevOpsService()
    service.release_preview_data["checks"] = [
        {
            "name": "tests",
            "command": ["python", "-m", "pytest", "-q"],
            "risk": "safe",
            "reason": "已识别",
        },
        {
            "name": "lint",
            "command": ["npm", "run", "lint"],
            "risk": "safe",
            "reason": "已识别",
        },
    ]
    approvals: list[tuple[str, RiskLevel, str]] = []
    provider = DevOpsToolProvider(
        service,  # type: ignore[arg-type]
        approval_mode="risk",
        approver=lambda *args: approvals.append(args) or False,
    )

    result = provider.execute("compose_release", {"version": "v1", "environment": "staging"})

    assert result.ok is False
    assert service.calls == []
    assert "pytest" in approvals[0][0]
    assert "npm" in approvals[0][2]


def test_unknown_release_check_requires_approval_even_in_full_mode() -> None:
    service = StubDevOpsService()
    service.release_preview_data["checks"] = [
        {
            "name": "custom",
            "command": ["custom-linter", "--strict"],
            "risk": "review",
            "reason": "未识别",
        }
    ]
    approvals: list[tuple[str, RiskLevel, str]] = []
    provider = DevOpsToolProvider(
        service,  # type: ignore[arg-type]
        approval_mode="full",
        approver=lambda *args: approvals.append(args) or True,
    )

    result = provider.execute("compose_release", {"version": "v1"})

    assert result.ok is True
    assert approvals
    assert service.calls == [("release", ("v1", None, None, "d" * 64, True))]


def test_denied_release_check_never_reaches_approver_or_service() -> None:
    service = StubDevOpsService()
    service.release_preview_data["checks"] = [
        {
            "name": "shell",
            "command": ["powershell", "-Command", "pytest"],
            "risk": "deny",
            "reason": "禁止 Shell",
        }
    ]
    approvals: list[tuple[str, RiskLevel, str]] = []
    provider = DevOpsToolProvider(
        service,  # type: ignore[arg-type]
        approval_mode="full",
        approver=lambda *args: approvals.append(args) or True,
    )

    result = provider.execute("compose_release", {"version": "v1"})

    assert result.ok is False
    assert "禁止命令" in (result.error or "")
    assert approvals == []
    assert service.calls == []


def test_full_mode_requires_approval_when_task_changed_release_config() -> None:
    service = StubDevOpsService()
    service.release_preview_data["checks"] = [
        {
            "name": "tests",
            "command": ["python", "-m", "pytest", "-q"],
            "risk": "safe",
            "reason": "已识别",
        }
    ]
    approvals: list[tuple[str, RiskLevel, str]] = []
    tracker = type("Tracker", (), {"changes": {"coding-agent.toml": object()}})()
    provider = DevOpsToolProvider(
        service,  # type: ignore[arg-type]
        approval_mode="full",
        approver=lambda *args: approvals.append(args) or False,
        change_tracker=tracker,  # type: ignore[arg-type]
    )

    result = provider.execute("compose_release", {"version": "v1"})

    assert result.ok is False
    assert approvals
    assert "修改了 coding-agent.toml" in approvals[0][2]
    assert service.calls == []


def test_rollback_always_requires_human_confirmation_even_in_full_mode() -> None:
    service = StubDevOpsService()
    approvals: list[tuple[str, RiskLevel, str]] = []
    provider = DevOpsToolProvider(
        service,  # type: ignore[arg-type]
        approval_mode="full",
        approver=lambda *args: approvals.append(args) or False,
    )

    result = provider.execute("compose_rollback", {"plan_id": "a" * 32})

    assert result.ok is False
    assert "未批准回滚" in (result.error or "")
    assert service.calls == [("rollback_preview", ("a" * 32,))]
    assert "production" in approvals[0][2]
    assert "v2" in approvals[0][2]
    assert "v1" in approvals[0][2]


def test_approved_rollback_uses_the_previewed_one_time_plan() -> None:
    service = StubDevOpsService()
    provider = DevOpsToolProvider(
        service,  # type: ignore[arg-type]
        approval_mode="full",
        approver=lambda _command, _risk, _reason: True,
    )

    result = provider.execute("compose_rollback", {"plan_id": "b" * 32})

    assert result.ok is True
    assert service.calls == [
        ("rollback_preview", ("b" * 32,)),
        ("rollback", ("b" * 32,)),
    ]


def test_argument_validation_happens_before_service_call() -> None:
    service = StubDevOpsService()
    provider = DevOpsToolProvider(service)  # type: ignore[arg-type]

    assert provider.execute("compose_logs", {"tail": 0}).ok is False
    assert provider.execute("compose_logs", {"extra": True}).error == "未知参数: extra"
    assert provider.execute("compose_build", {"services": "web"}).ok is False
    assert provider.execute("compose_release", {}).error == "缺少参数: version"
    assert provider.execute("compose_rollback", {"plan_id": "short"}).ok is False
    assert service.calls == []


def test_service_errors_remain_machine_readable_and_redacted_output_is_bounded() -> None:
    service = StubDevOpsService()
    service.failure = DevOpsOperationError("docker_unreachable", "无法连接", output="details")
    provider = DevOpsToolProvider(service, max_output=4)  # type: ignore[arg-type]

    result = provider.execute("compose_status", {})

    assert result.ok is False
    assert result.error == "docker_unreachable: 无法连接"
    assert result.output.startswith("deta")
