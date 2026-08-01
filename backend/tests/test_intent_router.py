"""LLMIntentRouter 意图分类路由测试（任务 1.1.3）.

全部用例通过 ``httpx.MockTransport`` 注入离线响应，不发起真实网络请求。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from core.intent_router import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    LLMIntentRouter,
    RuleBasedIntentRouter,
    default_router,
)
from core.state import TaskContext

# MockTransport 处理器类型
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 LLM 路由相关环境变量，避免用例受本机配置影响."""
    for name in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL):
        monkeypatch.delenv(name, raising=False)


def _make_router(
    handler: Handler,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> LLMIntentRouter:
    """构造注入 ``MockTransport`` 的 LLMIntentRouter."""

    def _client_factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=10)

    router = LLMIntentRouter(**kwargs)
    monkeypatch.setattr(router, "_create_client", _client_factory)
    return router


def _echo_router(
    label: str,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> tuple[LLMIntentRouter, list[httpx.Request]]:
    """构造固定返回指定标签的路由，并记录收到的全部请求."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": label}}]})

    return _make_router(handler, monkeypatch, **kwargs), captured


@pytest.mark.parametrize("label", ["teach", "learn", "evaluate"])
def test_llm_router_returns_valid_labels(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """模型返回合法标签时直接采用."""
    task = TaskContext(intent="teach", description="讲解一下拉格朗日中值定理")
    router, _ = _echo_router(label, monkeypatch)

    assert router.classify(task) == label


def test_llm_router_normalizes_quoted_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """带引号与空白的输出清洗后仍可命中合法标签."""
    router, _ = _echo_router('  "Learn"  ', monkeypatch)
    task = TaskContext(intent="teach", description="如何高效背单词")

    assert router.classify(task) == "learn"


def test_llm_router_falls_back_on_invalid_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型返回非法标签时回退规则路由."""
    router, captured = _echo_router("banana", monkeypatch)
    task = TaskContext(intent="evaluator", description="批改学生作业")

    assert router.classify(task) == "evaluator"
    assert len(captured) == 1


def test_llm_router_falls_back_on_http_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 500 错误时回退规则路由."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    router = _make_router(handler, monkeypatch)
    task = TaskContext(intent="learning_assistant", description="推荐学习计划")

    assert router.classify(task) == "learning_assistant"


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout],
)
def test_llm_router_falls_back_on_network_error(
    exc: type[httpx.TransportError],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网络错误与超时均回退规则路由."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("模拟网络异常")

    router = _make_router(handler, monkeypatch)
    task = TaskContext(intent="learning_assistant", description="推荐学习计划")

    assert router.classify(task) == "learning_assistant"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, text="not-json"), httpx.Response(200, json={})],
)
def test_llm_router_falls_back_on_parse_error(
    response: httpx.Response,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 JSON 与缺失字段均回退规则路由."""

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    router = _make_router(handler, monkeypatch)
    task = TaskContext(intent="learning_assistant", description="推荐学习计划")

    assert router.classify(task) == "learning_assistant"


def test_llm_router_handles_none_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """task 为 None 时用占位描述构造 prompt，模型输出优先."""
    router, captured = _echo_router("evaluate", monkeypatch)

    # 规则路由对 None 兜底返回 teaching_assistant，此处返回 evaluate 证明走了模型路径
    assert router.classify(None) == "evaluate"
    body = json.loads(captured[0].content)
    assert body["messages"][-1]["role"] == "user"
    assert "未知任务" in body["messages"][-1]["content"]


def test_llm_router_none_task_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """task 为 None 且模型输出非法时回退规则路由的默认值."""
    router, _ = _echo_router("banana", monkeypatch)

    assert router.classify(None) == "teaching_assistant"


def test_llm_router_uses_constructor_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置环境变量时使用默认 base_url 与 model."""
    router, captured = _echo_router("teach", monkeypatch)

    assert router.classify(TaskContext(intent="learn", description="测试")) == "teach"
    request = captured[0]
    assert str(request.url) == f"{DEFAULT_BASE_URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer "
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body["model"] == DEFAULT_MODEL
    assert body["temperature"] == 0
    assert [message["role"] for message in body["messages"]] == ["system", "user"]


def test_llm_router_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """base_url / api_key / model 支持环境变量覆盖."""
    monkeypatch.setenv(ENV_BASE_URL, "http://llm.example.test/v1")
    monkeypatch.setenv(ENV_API_KEY, "env-secret-key")
    monkeypatch.setenv(ENV_MODEL, "env-model")
    router, captured = _echo_router("learn", monkeypatch)

    assert router.classify(TaskContext(intent="teach", description="测试")) == "learn"
    request = captured[0]
    assert str(request.url) == "http://llm.example.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer env-secret-key"
    body = json.loads(request.content)
    assert body["model"] == "env-model"


def test_default_router_selects_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置 LLM_API_KEY 时选择 LLM 路由，否则选择规则路由."""
    assert isinstance(default_router(), RuleBasedIntentRouter)

    monkeypatch.setenv(ENV_API_KEY, "k")
    assert isinstance(default_router(), LLMIntentRouter)
