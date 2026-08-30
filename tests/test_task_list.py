from __future__ import annotations

import pytest

from coding_agent.context import ContextManager
from coding_agent.task_list import (
    TASK_LIST_ANCHOR_PREFIX,
    TaskListState,
    TaskListToolProvider,
)


def item(
    identifier: str,
    status: str,
    *,
    blocker: str | None = None,
) -> dict[str, object]:
    return {
        "id": identifier,
        "title": f"Step {identifier}",
        "status": status,
        "blocker": blocker,
    }


def turn(index: int) -> list[dict[str, object]]:
    return [
        {"role": "user", "content": f"user-{index}-" + "x" * 300},
        {"role": "assistant", "content": f"assistant-{index}-" + "y" * 300},
    ]


def test_state_enforces_one_active_item_and_blocker_reason() -> None:
    with pytest.raises(ValueError, match="最多只能有一个"):
        TaskListState(
            "Goal",
            [item("one", "in_progress"), item("two", "in_progress")],
        )
    with pytest.raises(ValueError, match="必须提供 blocker"):
        TaskListState("Goal", [item("one", "blocked")])

    state = TaskListState(
        "  Ship   feature ",
        [item("one", "completed"), item("two", "blocked", blocker="Need access")],
    )
    assert state.snapshot() == {
        "objective": "Ship feature",
        "items": [
            item("one", "completed"),
            item("two", "blocked", blocker="Need access"),
        ],
        "completed": 1,
        "total": 2,
        "blocked": 1,
    }


def test_update_tool_replaces_snapshot_and_notifies_persistence() -> None:
    state = TaskListState()
    updates: list[dict[str, object]] = []
    provider = TaskListToolProvider(state, on_update=updates.append)

    result = provider.execute(
        "update_task_list",
        {
            "objective": "Implement task state",
            "items": [item("design", "completed"), item("code", "in_progress")],
            "explanation": "Design is complete; implementation started.",
        },
    )

    assert result.ok is True
    assert updates and updates[0]["objective"] == "Implement task state"
    assert state.snapshot()["completed"] == 1
    assert state.system_message()["content"].startswith(TASK_LIST_ANCHOR_PREFIX)


def test_update_tool_rolls_back_when_persistence_fails() -> None:
    state = TaskListState("Original", [item("one", "in_progress")])

    def fail(_snapshot: dict[str, object]) -> None:
        raise OSError("disk full")

    result = TaskListToolProvider(state, on_update=fail).execute(
        "update_task_list",
        {
            "objective": "Changed",
            "items": [item("two", "pending")],
            "explanation": "Change plan",
        },
    )

    assert result.ok is False
    assert "保存失败" in str(result.error)
    assert state.snapshot()["objective"] == "Original"
    assert state.snapshot()["items"] == [item("one", "in_progress")]


def test_task_anchor_survives_context_compaction_unchanged() -> None:
    state = TaskListState("Stable goal", [item("one", "in_progress")])
    anchor = state.system_message()
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        anchor,
    ]
    for index in range(4):
        messages.extend(turn(index))

    compacted = ContextManager(500, keep_recent_turns=2).compact(
        messages, lambda _text: "rolling summary"
    )

    anchors = [message for message in compacted if TaskListState.is_anchor(message)]
    assert anchors == [anchor]
    assert compacted.index(anchor) < next(
        index for index, message in enumerate(compacted) if message.get("role") == "user"
    )


def test_invalid_stored_state_fails_closed_to_empty_list() -> None:
    restored = TaskListState.from_storage(
        {
            "objective": "Bad",
            "items": [item("one", "blocked")],
        }
    )
    assert restored.snapshot()["objective"] == ""
    assert restored.snapshot()["items"] == []
