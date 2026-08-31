from coding_agent.changes import ChangeSet
from coding_agent.execution_state import ExecutionState
from coding_agent.tools import ToolResult


def test_successful_validation_covers_current_mutation_revision() -> None:
    state = ExecutionState()
    state.begin_run()
    state.observe(
        "write_file",
        {"path": "a.py", "content": "pass\n"},
        ToolResult(True, changes=ChangeSet(("a.py",))),
    )

    assert state.needs_validation is True
    assert state.has_unreported_evidence_gap is True

    state.observe(
        "run_process",
        {"argv": ["python", "-m", "pytest", "-q"]},
        ToolResult(True, "exit_code=0\n1 passed"),
    )

    assert state.needs_validation is False
    assert state.verified_revision == 1
    assert state.completion_evidence()["validations"][0]["command"] == [
        "python",
        "-m",
        "pytest",
        "-q",
    ]


def test_new_mutation_expires_old_validation_and_unverified_report_is_not_repeated() -> None:
    state = ExecutionState()
    state.observe(
        "write_file", {}, ToolResult(True, changes=ChangeSet(("a.py",)))
    )
    state.observe(
        "run_command", {"command": "pytest -q"}, ToolResult(True, "exit_code=0")
    )
    state.observe(
        "replace_text", {}, ToolResult(True, changes=ChangeSet(("a.py",)))
    )

    assert state.needs_validation is True
    assert state.has_unreported_evidence_gap is True

    state.mark_completed(verified=False)

    assert state.outcome == "completed_unverified"
    assert state.has_unreported_evidence_gap is False


def test_failed_or_composed_commands_do_not_create_success_evidence() -> None:
    state = ExecutionState()
    state.observe(
        "write_file", {}, ToolResult(True, changes=ChangeSet(("a.py",)))
    )
    state.observe(
        "run_command", {"command": "pytest -q; echo fake"}, ToolResult(True)
    )
    state.observe(
        "run_process", {"argv": ["python", "-m", "pytest"]}, ToolResult(False)
    )

    assert state.needs_validation is True
    assert state.has_unreported_evidence_gap is True
    assert len(state.validations) == 1
    assert state.validations[0].succeeded is False


def test_failed_tool_that_changed_disk_still_advances_mutation_revision() -> None:
    state = ExecutionState()

    state.observe(
        "run_process",
        {"argv": ["python", "-m", "pytest"]},
        ToolResult(False, changes=ChangeSet(("generated.txt",))),
    )

    assert state.mutation_revision == 1
    assert state.validations[0].mutation_revision == 1
    assert state.needs_validation is True


def test_execution_state_storage_round_trip_is_bounded() -> None:
    state = ExecutionState()
    state.observe(
        "write_file", {}, ToolResult(True, changes=ChangeSet(("a.py",)))
    )
    state.observe(
        "run_process",
        {"argv": ["cargo", "check"]},
        ToolResult(True, "x" * 3_000),
    )
    state.mark_completed(verified=True)

    restored = ExecutionState.from_storage(state.to_storage())

    assert restored.to_storage() == state.to_storage()
    assert len(restored.validations[0].output_tail) == 2_000
