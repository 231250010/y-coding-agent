from pathlib import Path

import pytest

from coding_agent.local_settings import LocalSettings


def test_settings_round_trip_with_remembered_key(tmp_path: Path) -> None:
    settings = LocalSettings(
        api_key="local-test-value",
        model="deepseek-test",
        base_url="https://example.invalid",
        workspace=str(tmp_path),
        context_tokens=1234,
        max_steps=7,
        approval_mode="request",
        remember_key=True,
    )
    settings.save(tmp_path)
    loaded = LocalSettings.load(tmp_path)
    assert loaded.api_key == "local-test-value"
    assert loaded.model == "deepseek-test"
    assert loaded.max_steps == 7
    assert loaded.remember_key is True


def test_key_is_not_saved_when_remember_is_disabled(tmp_path: Path) -> None:
    settings = LocalSettings(api_key="temporary-test-value", workspace=str(tmp_path), remember_key=False)
    settings.save(tmp_path)
    text = LocalSettings.path(tmp_path).read_text(encoding="utf-8")
    assert "temporary-test-value" not in text
    assert LocalSettings.load(tmp_path).api_key == ""


def test_environment_overrides_saved_connection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    LocalSettings(model="saved-model", workspace=str(tmp_path)).save(tmp_path)
    monkeypatch.setenv("CODING_AGENT_API_KEY", "environment-test-value")
    monkeypatch.setenv("CODING_AGENT_MODEL", "environment-model")
    loaded = LocalSettings.load(tmp_path)
    assert loaded.api_key == "environment-test-value"
    assert loaded.model == "environment-model"


def test_invalid_settings_fall_back_to_defaults(tmp_path: Path) -> None:
    path = LocalSettings.path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"context_tokens":"bad","max_steps":-4}', encoding="utf-8")
    loaded = LocalSettings.load(tmp_path)
    assert loaded.context_tokens == 32_000
    assert loaded.max_steps == 20


def test_complete_connection_detection() -> None:
    assert LocalSettings(api_key="key", model="model", base_url="https://example.invalid").is_complete
    assert not LocalSettings(api_key="", model="model", base_url="https://example.invalid").is_complete


@pytest.mark.parametrize(("legacy", "expected"), [("ask", "risk"), ("always", "request")])
def test_legacy_approval_modes_are_migrated(tmp_path: Path, legacy: str, expected: str) -> None:
    path = LocalSettings.path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(f'{{"approval_mode":"{legacy}"}}', encoding="utf-8")

    assert LocalSettings.load(tmp_path).approval_mode == expected
