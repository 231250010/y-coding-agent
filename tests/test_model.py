from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from coding_agent.model import ModelError, OpenAIChatModel


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


def response(*, content: str | None = "done", calls: list[object] | None = None) -> object:
    message = SimpleNamespace(content=content, tool_calls=calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


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


def test_adapter_omits_tools_for_summary_requests() -> None:
    model, completions = fake_model([response()])
    model.complete([{"role": "user", "content": "summarize"}], None)
    assert "tools" not in completions.requests[0]
    assert "tool_choice" not in completions.requests[0]


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

