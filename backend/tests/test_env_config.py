"""app.py 护栏参数 env 解析测试（审查 S5：_env_positive_int /
_env_positive_float 的非法回退分支此前无守护）。"""

from __future__ import annotations

import pytest

from api.app import _env_positive_float, _env_positive_int


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
