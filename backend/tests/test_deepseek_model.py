"""DeepSeek 配置与模型创建测试。"""

from pathlib import Path

import pytest

from core.models.deepseek import DEFAULT_ENV_FILE, DeepSeekSettings, create_deepseek_model

ENV_KEYS = ("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY")


def clear_deepseek_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_env_file_points_to_project_root() -> None:
    expected = Path(__file__).resolve().parents[2] / ".env"

    assert DEFAULT_ENV_FILE == expected


def test_settings_load_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_deepseek_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_MODEL=deepseek-chat\n"
        "DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        "DEEPSEEK_API_KEY=test-key\n",
        encoding="utf-8",
    )

    settings = DeepSeekSettings.from_env(env_file)

    assert settings.model == "deepseek-chat"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.api_key == "test-key"
    assert "test-key" not in repr(settings)


def test_settings_require_all_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_deepseek_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_MODEL=deepseek-chat\n", encoding="utf-8")

    with pytest.raises(ValueError, match="DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY"):
        DeepSeekSettings.from_env(env_file)


def test_create_deepseek_model_uses_settings() -> None:
    settings = DeepSeekSettings(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="test-key",
    )

    model = create_deepseek_model(settings)

    assert model.model_name == "deepseek-chat"
    assert model.openai_api_base == "https://api.deepseek.com"
