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


def test_create_deepseek_model_default_limits_unchanged() -> None:
    """默认参数与原行为逐项一致（零回归）：timeout=60 / max_retries=1 /
    max_tokens=None（S5 可选覆盖参数的默认值锁定）。"""
    settings = DeepSeekSettings(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="test-key",
    )

    model = create_deepseek_model(settings)

    assert model.request_timeout == 60
    assert model.max_retries == 1
    assert model.max_tokens is None


def test_create_deepseek_model_accepts_lightweight_overrides() -> None:
    """S5 轻量实例：查询改写器等辅助链路用收紧的 timeout/max_tokens。"""
    settings = DeepSeekSettings(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="test-key",
    )

    model = create_deepseek_model(settings, timeout=10, max_retries=0, max_tokens=128)

    assert model.request_timeout == 10
    assert model.max_retries == 0
    assert model.max_tokens == 128
