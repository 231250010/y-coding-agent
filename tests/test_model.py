from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from coding_agent.model import ModelError, OpenAIChatModel


_MISSING = object()


class FakeCompletions:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_model(outcomes: list[object], sleeps: list[int] | None = None) -> tuple[OpenAIChatModel, FakeCompletions]:
    completions = FakeCompletions(outcomes)
    model = OpenAIChatModel.__new__(OpenAIChatModel)
    model._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model.model = "test-model"
    model.max_retries = 2
    model._sleep = (sleeps.append if sleeps is not None else lambda _seconds: None)
    return model, completions


def response(
    *,
    content: str | None = "done",
    calls: list[object] | None = None,
    reasoning_content: str | None | object = _MISSING,
) -> object:
    fields: dict[str, object] = {"content": content, "tool_calls": calls or []}
    if reasoning_content is not _MISSING:
        fields["reasoning_content"] = reasoning_content
    message = SimpleNamespace(**fields)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def chunk(
    *,
    content: str | None = None,
    calls: list[object] | None = None,
    finish_reason: str | None = None,
    reasoning_content: str | None | object = _MISSING,
) -> object:
    fields: dict[str, object] = {"content": content, "tool_calls": calls or []}
    if reasoning_content is not _MISSING:
        fields["reasoning_content"] = reasoning_content
    delta = SimpleNamespace(**fields)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    )


def test_adapter_parses_function_tool_calls() -> None:
    tool_call = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(name="read_file", arguments='{"path":"a.py"}'),
    )
    model, completions = fake_model([response(content=None, calls=[tool_call])])
    result = model.complete([{"role": "user", "content": "read"}], [{"type": "function"}])
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "read_file"
    assert completions.requests[0]["model"] == "test-model"
    assert completions.requests[0]["tool_choice"] == "auto"


def test_adapter_preserves_deepseek_reasoning_content() -> None:
    model, _ = fake_model(
        [response(content=None, reasoning_content="分析后决定调用工具")]
    )

    result = model.complete([{"role": "user", "content": "分析"}])

    assert result.reasoning_content == "分析后决定调用工具"


def test_adapter_reads_reasoning_content_from_sdk_model_extra() -> None:
    message = SimpleNamespace(
        content=None,
        tool_calls=[],
        model_extra={"reasoning_content": "兼容字段"},
    )
    raw_response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")]
    )
    model, _ = fake_model([raw_response])

    assert model.complete([{"role": "user", "content": "分析"}]).reasoning_content == (
        "兼容字段"
    )


def test_adapter_distinguishes_absent_and_explicit_null_reasoning_content() -> None:
    absent_model, _ = fake_model([response(content="plain")])
    null_model, _ = fake_model([response(content="thinking", reasoning_content=None)])

    absent = absent_model.complete([{"role": "user", "content": "plain"}])
    explicit_null = null_model.complete([{"role": "user", "content": "thinking"}])

    assert absent.reasoning_content is None
    assert absent.reasoning_content_present is False
    assert explicit_null.reasoning_content is None
    assert explicit_null.reasoning_content_present is True


def test_adapter_omits_tools_for_summary_requests() -> None:
    model, completions = fake_model([response()])
    model.complete([{"role": "user", "content": "summarize"}], None)
    assert "tools" not in completions.requests[0]
    assert "tool_choice" not in completions.requests[0]


def test_adapter_applies_hard_max_tokens_without_tools() -> None:
    model, completions = fake_model([response()])
    result = model.complete_with_max_tokens(
        [{"role": "user", "content": "bounded"}], 64
    )
    assert result.content == "done"
    assert completions.requests[0]["max_tokens"] == 64
    assert "tools" not in completions.requests[0]
    assert "tool_choice" not in completions.requests[0]


def test_adapter_rejects_invalid_hard_max_tokens() -> None:
    model, _ = fake_model([])
    with pytest.raises(ValueError, match="正整数"):
        model.complete_with_max_tokens([], 0)


def test_streaming_adapter_emits_text_and_reassembles_tool_calls() -> None:
    call_start = SimpleNamespace(
        index=0,
        id="call-1",
        function=SimpleNamespace(name="read_file", arguments='{"path":'),
    )
    call_end = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments='"a.py"}'),
    )
    model, completions = fake_model(
        [[
            chunk(reasoning_content="先分析"),
            chunk(content="正在", reasoning_content="再决定"),
            chunk(content="读取", calls=[call_start]),
            chunk(calls=[call_end], finish_reason="tool_calls"),
        ]]
    )
    deltas: list[str] = []

    result = model.complete_stream(
        [{"role": "user", "content": "read"}],
        [{"type": "function"}],
        deltas.append,
    )

    assert result.content == "正在读取"
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == '{"path":"a.py"}'
    assert result.reasoning_content == "先分析再决定"
    assert "".join(deltas) == "正在读取"
    assert completions.requests[0]["stream"] is True


def test_streaming_adapter_distinguishes_missing_and_null_reasoning_fields() -> None:
    absent_model, _ = fake_model([[chunk(content="plain", finish_reason="stop")]])
    null_model, _ = fake_model(
        [[
            chunk(reasoning_content=None),
            chunk(content="answer", finish_reason="stop"),
        ]]
    )

    absent = absent_model.complete_stream([], None, lambda _delta: None)
    explicit_null = null_model.complete_stream([], None, lambda _delta: None)

    assert absent.reasoning_content_present is False
    assert explicit_null.reasoning_content is None
    assert explicit_null.reasoning_content_present is True


def test_streaming_adapter_propagates_callback_abort_without_retry() -> None:
    model, completions = fake_model([[chunk(content="partial")]])

    def stop(_content: str) -> None:
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        model.complete_stream([{"role": "user", "content": "hello"}], None, stop)

    assert len(completions.requests) == 1


def test_streaming_adapter_retries_only_before_first_chunk() -> None:
    error = APIConnectionError(
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    )
    sleeps: list[int] = []
    model, completions = fake_model(
        [error, [chunk(content="recovered", finish_reason="stop")]], sleeps
    )

    result = model.complete_stream(
        [{"role": "user", "content": "hello"}], None, lambda _delta: None
    )

    assert result.content == "recovered"
    assert len(completions.requests) == 2
    assert sleeps == [1]


def test_streaming_adapter_does_not_retry_after_partial_transport_failure() -> None:
    error = APIConnectionError(
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    )

    def broken_stream() -> object:
        yield chunk(content="partial")
        raise error

    model, completions = fake_model([broken_stream()])
    deltas: list[str] = []

    with pytest.raises(ModelError, match="流式响应中断"):
        model.complete_stream(
            [{"role": "user", "content": "hello"}], None, deltas.append
        )

    assert deltas == ["partial"]
    assert len(completions.requests) == 1


def test_adapter_retries_connection_errors() -> None:
    error = APIConnectionError(request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"))
    sleeps: list[int] = []
    model, completions = fake_model([error, response()], sleeps)
    result = model.complete([{"role": "user", "content": "hello"}])
    assert result.content == "done"
    assert len(completions.requests) == 2
    assert sleeps == [1]


def test_adapter_rejects_empty_choices() -> None:
    model, _ = fake_model([SimpleNamespace(choices=[])])
    with pytest.raises(ModelError, match="空 choices"):
        model.complete([{"role": "user", "content": "hello"}])


def test_transport_can_initialize_with_socks_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")

    model = OpenAIChatModel(
        api_key="test-key",
        model="test-model",
        base_url="https://example.invalid",
    )

    assert model.model == "test-model"
