from pathlib import Path

import pytest

from coding_agent.config import Config, ConfigError


def test_config_reads_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-value")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://gateway.invalid/v1")
    monkeypatch.setenv("CODING_AGENT_CONTEXT_TOKENS", "12345")
    config = Config.from_values(workspace=tmp_path)
    assert config.api_key == "test-value"
    assert config.model == "test-model"
    assert config.base_url == "https://gateway.invalid/v1"
    assert config.context_tokens == 12345


def test_explicit_values_override_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODING_AGENT_API_KEY", "environment-value")
    monkeypatch.setenv("CODING_AGENT_MODEL", "environment-model")
    config = Config.from_values(api_key="explicit-value", model="explicit-model", workspace=tmp_path)
    assert config.api_key == "explicit-value"
    assert config.model == "explicit-model"


@pytest.mark.parametrize("missing", ["CODING_AGENT_API_KEY", "CODING_AGENT_MODEL"])
def test_required_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str) -> None:
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-value")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError):
        Config.from_values(workspace=tmp_path)


def test_invalid_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-value")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    with pytest.raises(ConfigError, match="工作区"):
        Config.from_values(workspace=tmp_path / "missing")

