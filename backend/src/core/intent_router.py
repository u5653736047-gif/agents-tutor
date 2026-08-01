"""意图分类路由（任务 1.1.3）.

Supervisor 依赖一个 ``IntentRouter`` 将用户请求分类为教学场景意图，
从而决定分派给哪个子 Agent。本模块提供：

- ``IntentRouter``：路由协议（可注入，便于测试与替换）
- ``RuleBasedIntentRouter``：基于 ``TaskContext.intent`` 标签的规则路由（离线兜底）
- ``LLMIntentRouter``：基于 LLM 的分类路由（OpenAI 兼容端点，默认 DeepSeek）
- ``default_router``：依据环境变量自动选择 LLM 或规则实现

LLM 配置（模型名 / base_url / api_key）统一从项目根 ``.env`` 读取
（``DEEPSEEK_MODEL`` / ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY``），
见 ``core.config``。
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

from core.config import load_env
from core.state import AgentRole, TaskContext

# 模块导入时加载项目根 .env（幂等；已存在的环境变量优先，不会被覆盖）
load_env()

# 合法意图标签集合（与 StateGraph 节点一一对应）
VALID_INTENTS: frozenset[str] = frozenset(
    {AgentRole.TEACHING_ASSISTANT.value, AgentRole.LEARNING_ASSISTANT.value, AgentRole.EVALUATOR.value}
)

# LLM 输出使用的教学场景意图标签；与 VALID_INTENTS（节点名集合）并列，
# 两者任一命中均视为合法，保证模型输出与规则兜底返回值都能被采用
_INTENT_LABELS: frozenset[str] = frozenset({"teach", "learn", "evaluate"})

# 短意图标签 → 节点名 别名映射（兼容 1.1.2 的短标签体系；路由结果统一
# 经此表转换为节点名后交给 Supervisor 分派，是短标签的唯一权威来源）
INTENT_ALIASES: dict[str, str] = {
    "teach": AgentRole.TEACHING_ASSISTANT.value,
    "learn": AgentRole.LEARNING_ASSISTANT.value,
    "evaluate": AgentRole.EVALUATOR.value,
}

# LLM 路由的环境变量配置（统一来自项目根 .env 的 DeepSeek 配置）
ENV_BASE_URL = "DEEPSEEK_BASE_URL"
ENV_API_KEY = "DEEPSEEK_API_KEY"
ENV_MODEL = "DEEPSEEK_MODEL"

# 未配置 .env / 环境变量时的默认值（DeepSeek 官方兼容端点）
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class IntentRouter(Protocol):
    """意图分类路由协议."""

    def classify(self, task: TaskContext | None) -> str:
        """返回合法意图标签；无法识别时返回任意非合法值（由调用方兜底）. """


class RuleBasedIntentRouter:
    """基于任务意图标签的规则路由.

    按 ``task.intent`` 查表分派（同时兼容节点名与短意图标签），
    未知意图默认交给助教 Agent 兜底。
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {
            **{name: name for name in VALID_INTENTS},
            **INTENT_ALIASES,
        }

    def classify(self, task: TaskContext | None) -> str:
        intent = task.intent if task else ""
        return self._mapping.get(intent, AgentRole.TEACHING_ASSISTANT.value)


class LLMIntentRouter:
    """基于 LLM 的意图分类路由（OpenAI 兼容 Chat Completions API）.

    通过 HTTP POST 调用 ``{base_url}/chat/completions``，要求模型返回
    ``teach`` / ``learn`` / ``evaluate`` 之一；解析失败或网络异常时
    回退到 ``fallback`` 路由，保证分类永不抛异常。

    Args:
        base_url: API 基础地址（不含 /chat/completions 后缀）
        api_key: 鉴权密钥
        model: 模型名称
        timeout: 请求超时秒数
        fallback: 失败时的兜底路由，默认规则路由
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 10.0,
        fallback: IntentRouter | None = None,
    ) -> None:
        self._base_url = (base_url or os.getenv(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key or os.getenv(ENV_API_KEY) or ""
        self._model = model or os.getenv(ENV_MODEL) or DEFAULT_MODEL
        self._timeout = timeout
        self._fallback = fallback or RuleBasedIntentRouter()

    def classify(self, task: TaskContext | None) -> str:
        """LLM 分类意图；任何失败回退规则路由. """

        try:
            label = self._request_label(task)
        # 需求约定：网络/超时/HTTP/解析/JSON 等任何异常一律捕获并回退，禁止外抛
        except Exception:  # noqa: BLE001
            label = ""
        if label in VALID_INTENTS or label in _INTENT_LABELS:
            return label
        return self._fallback.classify(task)

    def _request_label(self, task: TaskContext | None) -> str:
        """调用 OpenAI 兼容端点请求一次分类，异常交由 ``classify`` 兜底. """

        payload = {
            "model": self._model,
            "messages": self._build_messages(task),
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        with self._create_client() as client:
            response = client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        return self._normalize_label(content)

    def _build_messages(self, task: TaskContext | None) -> list[dict[str, str]]:
        """构造 system/user 消息，约束模型只能返回一个意图标签. """

        description = task.description if task else "未知任务"
        return [
            {
                "role": "system",
                "content": "你是教学场景意图分类器，只能从 teach、learn、evaluate 三个标签中选择一个，不要输出任何其他内容。",
            },
            {
                "role": "user",
                "content": f"任务描述：{description}\n该任务属于哪个意图？只返回标签本身。",
            },
        ]

    def _normalize_label(self, content: object) -> str:
        """清洗模型输出：去空白与引号并转小写，非字符串视为非法. """

        if not isinstance(content, str):
            return ""
        return content.strip().strip("\"'`").strip().lower()

    def _create_client(self) -> httpx.Client:
        """创建同步 HTTP 客户端，测试可注入 ``httpx.MockTransport``. """

        return httpx.Client(timeout=self._timeout)


def default_router() -> IntentRouter:
    """按环境变量选择路由实现.

    配置了 ``DEEPSEEK_API_KEY``（来自 .env 或环境变量）时返回带规则兜底的
    LLM 路由，否则返回纯规则路由。
    """
    if os.getenv(ENV_API_KEY):
        return LLMIntentRouter()
    return RuleBasedIntentRouter()
