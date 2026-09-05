"""app.py 护栏参数 env 解析测试（审查 S5：_env_positive_int /
_env_positive_float 的非法回退分支此前无守护；S5 增补 _env_mode
枚举开关的校验分支）。"""

from __future__ import annotations

import pytest

from api.app import _env_mode, _env_positive_float, _env_positive_int


@pytest.mark.parametrize(
    "raw,expected",
    [("524288", 524288), (" 200 ", 200), ("1", 1)],
    ids=["tokens", "messages-with-space", "one"],
)
def test_env_positive_int_accepts_valid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("TEST_GUARD_INT", raw)
    assert _env_positive_int("TEST_GUARD_INT", 42) == expected


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "3.5"], ids=["non-int", "zero", "negative", "float"])
def test_env_positive_int_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    monkeypatch.setenv("TEST_GUARD_INT", raw)
    with caplog.at_level("WARNING"):
        assert _env_positive_int("TEST_GUARD_INT", 42) == 42
    assert "TEST_GUARD_INT" in caplog.text


def test_env_positive_int_missing_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_GUARD_INT", raising=False)
    assert _env_positive_int("TEST_GUARD_INT", 42) == 42


@pytest.mark.parametrize(
    "raw,expected",
    [("0.5", 0.5), (" 0.01 ", 0.01)],
    ids=["half", "threshold-with-space"],
)
def test_env_positive_float_accepts_valid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("TEST_GUARD_FLOAT", raw)
    assert _env_positive_float("TEST_GUARD_FLOAT", 9.9) == expected


@pytest.mark.parametrize(
    "raw",
    ["0,01", "abc", "0", "-1", "nan", "inf", "-inf", "1e999"],
    ids=[
        "comma-typo",
        "non-float",
        "zero",
        "negative",
        "nan-silently-disables",
        "inf-rejects-all",
        "neg-inf",
        "overflow-to-inf",
    ],
)
def test_env_positive_float_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    """审查 S4 的核心场景：拼写错误不崩启动、nan/inf 不静默失效。"""
    monkeypatch.setenv("TEST_GUARD_FLOAT", raw)
    with caplog.at_level("WARNING"):
        assert _env_positive_float("TEST_GUARD_FLOAT", 0.01) == 0.01
    assert "TEST_GUARD_FLOAT" in caplog.text


def test_env_positive_float_missing_returns_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_GUARD_FLOAT", raising=False)
    assert _env_positive_float("TEST_GUARD_FLOAT", 0.01) == 0.01


# ── S5：_env_mode 枚举开关（API_KNOWLEDGE_REWRITE / API_KNOWLEDGE_RERANK）──


@pytest.mark.parametrize("raw", ["auto", " off ", "AUTO"], ids=["plain", "with-space", "uppercase"])
def test_env_mode_accepts_valid_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """合法枚举值（含首尾空白）被接受；大小写敏感（约定小写）。"""
    monkeypatch.setenv("TEST_MODE", raw)
    result = _env_mode("TEST_MODE", "auto", frozenset({"auto", "off", "AUTO"}))
    assert result == raw.strip()


def test_env_mode_missing_or_blank_returns_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未设置 / 空白 → 回退默认值（空白视为未配置，不报错）。"""
    monkeypatch.delenv("TEST_MODE", raising=False)
    assert _env_mode("TEST_MODE", "auto", frozenset({"auto", "off"})) == "auto"
    monkeypatch.setenv("TEST_MODE", "   ")
    assert _env_mode("TEST_MODE", "auto", frozenset({"auto", "off"})) == "auto"


def test_env_mode_invalid_value_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法值（拼写错误）→ ValueError：配置错误要暴露，不静默回退。

    与 _env_positive_int/float 的「非法回退默认」刻意不同：模式开关是
    能力配置而非护栏参数（见 app.py _env_mode 注释）。
    """
    monkeypatch.setenv("TEST_MODE", "auot")  # 拼写错误
    with pytest.raises(ValueError, match="TEST_MODE"):
        _env_mode("TEST_MODE", "auto", frozenset({"auto", "off"}))
