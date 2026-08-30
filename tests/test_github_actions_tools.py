from __future__ import annotations

import json
from typing import Any

from coding_agent.github_actions_tools import GitHubActionsToolProvider
from coding_agent.safety import RiskLevel


class StubService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def status(self, commit: str | None, *, limit: int) -> dict[str, Any]:
        self.calls.append(("status", (commit, limit)))
        return {"overall": "success"}

    def failed_logs(self, run_id: int, *, max_chars: int) -> dict[str, Any]:
        self.calls.append(("logs", (run_id, max_chars)))
        return {"run_id": run_id, "logs": "failed"}

    def rerun_failed(self, run_id: int) -> dict[str, Any]:
        self.calls.append(("rerun", (run_id,)))
        return {"run_id": run_id, "status": "rerun_requested"}


def test_exposes_actions_status_logs_and_rerun() -> None:
    provider = GitHubActionsToolProvider(StubService())  # type: ignore[arg-type]

    assert [item["function"]["name"] for item in provider.schemas()] == [
        "github_actions_status",
        "github_actions_failed_logs",
        "github_actions_rerun_failed",
    ]


def test_status_and_logs_are_read_only() -> None:
    service = StubService()
    provider = GitHubActionsToolProvider(service)  # type: ignore[arg-type]

    status = provider.execute("github_actions_status", {"commit": "a" * 40, "limit": 5})
    logs = provider.execute("github_actions_failed_logs", {"run_id": 10})

    assert json.loads(status.output)["overall"] == "success"
    assert logs.ok is True
    assert service.calls == [("status", ("a" * 40, 5)), ("logs", (10, 12000))]


def test_rerun_always_requires_human_confirmation() -> None:
    service = StubService()
    approvals: list[tuple[str, RiskLevel, str]] = []
    provider = GitHubActionsToolProvider(
        service,  # type: ignore[arg-type]
        approver=lambda *args: approvals.append(args) or False,
    )

    result = provider.execute("github_actions_rerun_failed", {"run_id": 77})

    assert result.ok is False
    assert service.calls == []
    assert "77" in approvals[0][0]


def test_approved_rerun_is_dispatched() -> None:
    service = StubService()
    provider = GitHubActionsToolProvider(
        service,  # type: ignore[arg-type]
        approver=lambda _command, _risk, _reason: True,
    )

    result = provider.execute("github_actions_rerun_failed", {"run_id": 77})

    assert result.ok is True
    assert service.calls == [("rerun", (77,))]


def test_argument_validation_rejects_invalid_run_id() -> None:
    service = StubService()
    provider = GitHubActionsToolProvider(service)  # type: ignore[arg-type]

    assert provider.execute("github_actions_failed_logs", {"run_id": 0}).ok is False
    assert provider.execute("github_actions_status", {"commit": "no"}).ok is False
    assert service.calls == []
