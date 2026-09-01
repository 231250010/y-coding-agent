from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .tools import ToolResult


MAX_VALIDATION_RECORDS = 20
MAX_VALIDATION_OUTPUT_CHARS = 2_000
_SHELL_COMPOSITION = re.compile(r"[|><;&`(){}]|\$\(|@\(")


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    command: tuple[str, ...]
    mutation_revision: int
    succeeded: bool
    finished_at: str
    output_tail: str = ""

    def to_storage(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "mutation_revision": self.mutation_revision,
            "succeeded": self.succeeded,
            "finished_at": self.finished_at,
            "output_tail": self.output_tail,
        }


@dataclass(slots=True)
class ExecutionState:
    mutation_revision: int = 0
    verified_revision: int = 0
    reported_unverified_revision: int = 0
    validation_attempts: int = 0
    reported_validation_attempt: int = 0
    validations: list[ValidationRecord] = field(default_factory=list)
    outcome: str = "idle"
    configured_validation_commands: tuple[tuple[str, ...], ...] = field(
        default=(), repr=False
    )

    def configure_validation_commands(
        self, commands: Sequence[Sequence[str]]
    ) -> None:
        self.configured_validation_commands = tuple(tuple(command) for command in commands)

    def begin_run(self) -> None:
        self.outcome = "running"

    @property
    def needs_validation(self) -> bool:
        latest = next(
            (
                record
                for record in reversed(self.validations)
                if record.mutation_revision == self.mutation_revision
            ),
            None,
        )
        if latest is not None:
            return not latest.succeeded
        return self.mutation_revision > self.verified_revision

    @property
    def has_unreported_evidence_gap(self) -> bool:
        return self.needs_validation and (
            self.mutation_revision > self.reported_unverified_revision
            or self.validation_attempts > self.reported_validation_attempt
        )

    def observe(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        # A command may modify files before returning a non-zero exit code.
        # Disk evidence, rather than the tool's success flag, defines mutation.
        if result.changes.paths:
            self.mutation_revision += 1
        command = self._validation_command(tool_name, arguments)
        if command is None:
            return
        self.validation_attempts += 1
        record = ValidationRecord(
            command=command,
            mutation_revision=self.mutation_revision,
            succeeded=result.ok,
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            output_tail=result.output[-MAX_VALIDATION_OUTPUT_CHARS:],
        )
        self.validations.append(record)
        del self.validations[:-MAX_VALIDATION_RECORDS]
        if result.ok:
            self.verified_revision = self.mutation_revision

    def mark_completed(self, *, verified: bool) -> None:
        self.outcome = "completed" if verified else "completed_unverified"
        if not verified:
            self.reported_unverified_revision = self.mutation_revision
            self.reported_validation_attempt = self.validation_attempts

    def mark_failed(self) -> None:
        self.outcome = "failed"

    def mark_cancelled(self) -> None:
        self.outcome = "cancelled"

    def completion_evidence(self) -> dict[str, Any]:
        current = [
            record.to_storage()
            for record in self.validations
            if record.succeeded and record.mutation_revision == self.mutation_revision
        ]
        return {
            "outcome": self.outcome,
            "mutation_revision": self.mutation_revision,
            "verified_revision": self.verified_revision,
            "reported_unverified_revision": self.reported_unverified_revision,
            "validation_attempts": self.validation_attempts,
            "reported_validation_attempt": self.reported_validation_attempt,
            "needs_validation": self.needs_validation,
            "validations": current,
        }

    def to_storage(self) -> dict[str, Any]:
        return {
            "mutation_revision": self.mutation_revision,
            "verified_revision": self.verified_revision,
            "reported_unverified_revision": self.reported_unverified_revision,
            "validation_attempts": self.validation_attempts,
            "reported_validation_attempt": self.reported_validation_attempt,
            "validations": [record.to_storage() for record in self.validations],
            "outcome": self.outcome,
        }

    @classmethod
    def from_storage(cls, value: Any) -> ExecutionState:
        if not isinstance(value, dict):
            return cls()
        mutation = cls._non_negative_int(value.get("mutation_revision"))
        verified = min(cls._non_negative_int(value.get("verified_revision")), mutation)
        reported = min(
            cls._non_negative_int(value.get("reported_unverified_revision")), mutation
        )
        attempts = cls._non_negative_int(value.get("validation_attempts"))
        reported_attempt = min(
            cls._non_negative_int(value.get("reported_validation_attempt")), attempts
        )
        raw_records = value.get("validations")
        records: list[ValidationRecord] = []
        if isinstance(raw_records, list):
            for raw in raw_records[-MAX_VALIDATION_RECORDS:]:
                record = cls._record_from_storage(raw)
                if record is not None and record.mutation_revision <= mutation:
                    records.append(record)
        outcome = value.get("outcome")
        if outcome not in {
            "idle",
            "running",
            "completed",
            "completed_unverified",
            "failed",
            "cancelled",
        }:
            outcome = "idle"
        return cls(
            mutation_revision=mutation,
            verified_revision=verified,
            reported_unverified_revision=reported,
            validation_attempts=attempts,
            reported_validation_attempt=reported_attempt,
            validations=records,
            outcome=outcome,
        )

    @staticmethod
    def _record_from_storage(value: Any) -> ValidationRecord | None:
        if not isinstance(value, dict):
            return None
        command = value.get("command")
        revision = value.get("mutation_revision")
        succeeded = value.get("succeeded")
        finished_at = value.get("finished_at")
        output_tail = value.get("output_tail", "")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 30
            or any(not isinstance(item, str) or not item for item in command)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or not isinstance(succeeded, bool)
            or not isinstance(finished_at, str)
            or not isinstance(output_tail, str)
        ):
            return None
        return ValidationRecord(
            tuple(command),
            revision,
            succeeded,
            finished_at[:40],
            output_tail[-MAX_VALIDATION_OUTPUT_CHARS:],
        )

    @staticmethod
    def _non_negative_int(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _validation_command(
        self,
        tool_name: str, arguments: dict[str, Any]
    ) -> tuple[str, ...] | None:
        if tool_name == "run_process":
            raw = arguments.get("argv")
            if not isinstance(raw, list):
                return None
            command = tuple(item for item in raw if isinstance(item, str))
            return command if (
                command in self.configured_validation_commands
                or ExecutionState._is_validation_argv(command)
            ) else None
        if tool_name == "run_command":
            raw = arguments.get("command")
            if not isinstance(raw, str) or _SHELL_COMPOSITION.search(raw):
                return None
            # This split is only used to recognize and display a validation;
            # execution still follows the original shell command semantics.
            command = tuple(raw.strip().split())
            return command if ExecutionState._is_validation_argv(command) else None
        if tool_name == "compose_verify":
            return ("compose_verify",)
        return None

    @staticmethod
    def _is_validation_argv(command: Sequence[str]) -> bool:
        if not command or len(command) > 30:
            return False
        executable = Path(command[0]).name.casefold()
        rest = [item.casefold() for item in command[1:]]
        if executable in {"pytest", "pytest.exe"}:
            return True
        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            return len(rest) >= 2 and rest[0] == "-m" and rest[1] in {
                "pytest",
                "unittest",
                "compileall",
                "py_compile",
            }
        if executable in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}:
            return bool(rest) and (
                rest[0] == "test"
                or (len(rest) >= 2 and rest[0] == "run" and rest[1] in {"test", "build", "lint"})
            )
        if executable in {"cargo", "cargo.exe"}:
            return bool(rest) and rest[0] in {"test", "check", "build"}
        if executable in {"go", "go.exe"}:
            return bool(rest) and rest[0] in {"test", "build"}
        return False
