from coding_agent.context import ContextManager


def make_turn(index: int, size: int = 100) -> list[dict[str, object]]:
    return [
        {"role": "user", "content": f"task-{index}-" + "x" * size},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": f"call-{index}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": f"call-{index}", "content": "result-" + "y" * size},
        {"role": "assistant", "content": f"done-{index}"},
    ]


def test_context_under_budget_is_unchanged() -> None:
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]
    manager = ContextManager(10_000)
    assert manager.compact(messages, lambda _text: "unused") == messages


def test_context_summarizes_old_complete_turns() -> None:
    messages = [{"role": "system", "content": "system"}]
    for index in range(4):
        messages.extend(make_turn(index, 300))
    seen: list[str] = []
    manager = ContextManager(500, keep_recent_turns=2)
    compacted = manager.compact(messages, lambda text: seen.append(text) or "state summary")
    assert seen and "task-0" in seen[0] and "task-1" in seen[0]
    assert any(message.get("content") == "较早会话摘要：\nstate summary" for message in compacted)
    assert any("task-2" in str(message.get("content")) for message in compacted)
    assert any("task-3" in str(message.get("content")) for message in compacted)
    # A retained assistant tool call always keeps its matching tool response.
    call_ids = {
        call["id"]
        for message in compacted
        for call in message.get("tool_calls", [])  # type: ignore[union-attr]
    }
    response_ids = {message["tool_call_id"] for message in compacted if message.get("role") == "tool"}
    assert call_ids == response_ids


def test_summary_failure_uses_fallback_notice() -> None:
    messages = [{"role": "system", "content": "system"}]
    for index in range(3):
        messages.extend(make_turn(index, 300))
    manager = ContextManager(400, keep_recent_turns=1)

    def fail(_text: str) -> str:
        raise RuntimeError("offline")

    compacted = manager.compact(messages, fail)
    assert any("已被裁剪" in str(message.get("content")) for message in compacted)
    assert any("task-2" in str(message.get("content")) for message in compacted)


def test_large_tool_output_is_truncated_without_splitting() -> None:
    messages = [{"role": "system", "content": "s"}] + make_turn(0, 10_000)
    manager = ContextManager(100)
    compacted = manager.compact(messages, lambda _text: "summary")
    tool = next(message for message in compacted if message.get("role") == "tool")
    assert "已截断" in str(tool["content"])

