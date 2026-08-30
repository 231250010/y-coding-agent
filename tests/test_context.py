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


def test_recent_tool_output_stays_complete_below_hard_limit() -> None:
    messages = [{"role": "system", "content": "s"}] + make_turn(0, 4_500)
    original = str(messages[3]["content"])
    manager = ContextManager(8_000, trigger_ratio=0.5)

    compacted = manager.compact(messages, lambda _text: "summary")

    tool = next(message for message in compacted if message.get("role") == "tool")
    assert tool["content"] == original


def test_recent_tool_output_keeps_head_and_tail_at_hard_limit() -> None:
    messages = [{"role": "system", "content": "s"}] + make_turn(0, 10)
    content = "HEAD-IMPORTANT\n" + "x" * 10_000 + "\nTAIL-ERROR"
    messages[3]["content"] = content
    manager = ContextManager(500)

    compacted = manager.compact(messages, lambda _text: "summary")

    tool = next(message for message in compacted if message.get("role") == "tool")
    result = str(tool["content"])
    assert result.startswith("HEAD-IMPORTANT")
    assert result.endswith("TAIL-ERROR")
    assert "近期工具输出因硬性上下文上限已截断" in result
    assert manager.estimate_tokens(compacted) <= manager.max_tokens


def test_old_tool_output_is_bounded_before_summary_and_keeps_tail() -> None:
    old_turn = make_turn(0, 10)
    old_turn[2]["content"] = "OLD-HEAD\n" + "z" * 10_000 + "\nOLD-TAIL"
    messages = (
        [{"role": "system", "content": "s"}]
        + old_turn
        + make_turn(1, 100)
        + make_turn(2, 100)
    )
    seen: list[str] = []

    ContextManager(1_000, keep_recent_turns=2).compact(
        messages, lambda text: seen.append(text) or "summary"
    )

    assert seen
    assert "OLD-HEAD" in seen[0]
    assert "OLD-TAIL" in seen[0]
    assert "旧工具输出为生成摘要已压缩" in seen[0]


def test_repeated_compaction_replaces_old_summary_with_one_rolling_note() -> None:
    manager = ContextManager(500, keep_recent_turns=2)
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "system", "content": "persistent policy"},
    ]
    for index in range(4):
        messages.extend(make_turn(index, 300))

    first = manager.compact(messages, lambda _text: "summary-v1")
    for index in range(4, 6):
        first.extend(make_turn(index, 300))
    seen: list[str] = []
    second = manager.compact(
        first, lambda text: seen.append(text) or "summary-v2"
    )

    notes = [
        message
        for message in second
        if str(message.get("content") or "").startswith("较早会话摘要：")
    ]
    assert [message["content"] for message in notes] == ["较早会话摘要：\nsummary-v2"]
    assert seen and "summary-v1" in seen[0]
    assert "task-2" in seen[0] and "task-3" in seen[0]
    assert any(message.get("content") == "persistent policy" for message in second)
    assert not any(message.get("content") == "较早会话摘要：\nsummary-v1" for message in second)


def test_compaction_coalesces_legacy_multiple_notes_and_fallbacks() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "system", "content": "较早会话摘要：\nold-a"},
        {
            "role": "system",
            "content": "较早会话因上下文预算已被裁剪；请依据当前工作区和近期消息继续。",
        },
    ]
    for index in range(3):
        messages.extend(make_turn(index, 300))
    seen: list[str] = []

    compacted = ContextManager(400, keep_recent_turns=1).compact(
        messages, lambda text: seen.append(text) or "merged"
    )

    system_contents = [
        str(message.get("content") or "")
        for message in compacted
        if message.get("role") == "system"
    ]
    assert system_contents == ["system", "较早会话摘要：\nmerged"]
    assert "old-a" in seen[0]
    assert "较早会话因上下文预算已被裁剪" in seen[0]


def test_repeated_compaction_failure_discards_stale_summary_note() -> None:
    manager = ContextManager(500, keep_recent_turns=2)
    messages: list[dict[str, object]] = [{"role": "system", "content": "system"}]
    for index in range(4):
        messages.extend(make_turn(index, 300))
    first = manager.compact(messages, lambda _text: "stale summary")
    for index in range(4, 6):
        first.extend(make_turn(index, 300))

    def fail(_text: str) -> str:
        raise RuntimeError("offline")

    compacted = manager.compact(first, fail)
    system_contents = [
        str(message.get("content") or "")
        for message in compacted
        if message.get("role") == "system"
    ]
    assert system_contents == [
        "system",
        "较早会话因上下文预算已被裁剪；请依据当前工作区和近期消息继续。",
    ]
    assert "stale summary" not in "\n".join(system_contents)
