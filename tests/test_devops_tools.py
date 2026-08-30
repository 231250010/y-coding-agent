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


def test_argument_validation_happens_before_service_call() -> None:
    service = StubDevOpsService()
    provider = DevOpsToolProvider(service)  # type: ignore[arg-type]

    assert provider.execute("compose_logs", {"tail": 0}).ok is False
    assert provider.execute("compose_logs", {"extra": True}).error == "未知参数: extra"
    assert provider.execute("compose_build", {"services": "web"}).ok is False
    assert service.calls == []


def test_service_errors_remain_machine_readable_and_redacted_output_is_bounded() -> None:
    service = StubDevOpsService()
    service.failure = DevOpsOperationError("docker_unreachable", "无法连接", output="details")
    provider = DevOpsToolProvider(service, max_output=4)  # type: ignore[arg-type]

    result = provider.execute("compose_status", {})

    assert result.ok is False
    assert result.error == "docker_unreachable: 无法连接"
    assert result.output.startswith("deta")

