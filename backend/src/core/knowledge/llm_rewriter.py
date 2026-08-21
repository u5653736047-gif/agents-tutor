"""LLM 查询改写器：QueryRewriter 协议（retrieval.py）的真实模型实现。

（面向初学者的设计说明，按功能模块）

1. 本模块的位置：检索编排层的可选增强组件
   retrieval.py 定义了 QueryRewriter 协议与「改写失败降级为原始 query」
   的安全网（_safe_variants），但刻意不实现真实 LLM 改写（见该模块注释
   第 6 节：模型调用属于上层关注点）。本模块就是这个「上层实现」：
   生产装配（api/app.py）把模型注入本改写器，再经 KnowledgeService
   构造参数进入检索链路——knowledge 包不反向依赖 nodes/Agent 层，
   只依赖一个本地声明的最小模型协议（_RewriterModel）。

2. 为什么不用 function calling
   改写产出是「几个查询字符串」，用纯文本换行输出即可表达；让模型走
   工具调用协议会多一轮 schema 解析与失败面（模型可能不调工具、参数
   缺字段等），没有任何收益。换行文本 + 规则解析（去序号/去引号）是
   成本最低、失败面最小的形态；解析为空或模型异常时直接抛错，交给
   检索层的既有降级语义兜底（降级为原始 query 单路检索，不阻断）。

3. 改写契约：召回下限不退化
   返回列表永远以「原查询」开头（模型产出中已含原查询时去重），模型
   变体只作增补。原因：多变体合并按 chunk 取最高分（retrieval.py
   第 4 节 max 合并），保留原查询意味着「改写后的联合检索结果不差于
   单查原查询」——改写是纯增益，不存在模型乱改导致原查询词丢失、
   召回反而退化的风险。

4. 有界 LRU 缓存
   同一 query 在多轮对话与 refine 重检中可能重复出现，每次都调模型
   既费钱又费延迟。缓存以「去除首尾空白的 query」为键，命中即直接
   返回；容量有界（默认 128），超限弹出最久未使用的条目。
   线程安全：search_knowledge 工具在 FastAPI 工作线程池中并发执行，
   缓存的「读-改-写」序列必须用锁串行化（OrderedDict 单步操作靠 GIL
   原子，但 move_to_end 与弹出的组合不是）；锁只包缓存访问，模型调用
   在锁外，慢调用不阻塞其他线程的缓存命中。

5. 延迟与失败预算
   装配侧（api/app.py）为本组件使用独立轻量模型实例（timeout/max_tokens
   收紧），与主对话模型互不影响；改写发生在 search_knowledge 工具调用
   时点（ReAct 中间轮），不在回答的首 token 路径上。
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

# 变体行解析：剥掉模型常见的列表编号/项目符号前缀（"1." / "2、" / "- " 等）。
_NUMBERING_PREFIX = re.compile(r"^\s*(?:\d{1,2}\s*[.、．:：)]|[-*•▪·])\s*")
# 变体两端可能出现的成对引号/书名号（模型偶尔会给变体加引号）。
_VARIANT_WRAPPERS = " \t\"'“”‘’「」『』《》"
# 防御上限：超过该长度的「变体行」几乎不可能是检索查询（模型把解释
# 段落混进输出），直接丢弃而不是把大段文本当查询送进索引。
_MAX_VARIANT_CHARS = 200

_REWRITE_SYSTEM_PROMPT = (
    "你是知识库检索的查询改写器。给定用户问题，产出用于检索的查询变体："
    "保持原意，从同义表述、术语规范化、上下位概念等角度改写。"
    "规则：只输出查询文本，每个变体占一行；不要编号、不要解释、"
    "不要复述问题、不要输出任何其他内容。"
)


class _RewriterModel(Protocol):
    """改写器需要的最小模型接口（与 nodes/react_agent.ChatModel 同形）。

    在 knowledge 包内本地声明而不是 import Agent 层的协议：保持
    knowledge → Agent 的零依赖方向（见模块注释第 1 节）。任何提供
    invoke(messages) -> AIMessage 的对象都满足本协议（鸭子类型）。
    """

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        """根据消息生成文本回答。"""
        ...


def _message_text(message: AIMessage) -> str:
    """提取模型回答的纯文本；content 为内容块列表时拼接其中的文本块。

    读取端宽容（与 state.py 的读取哲学一致）：部分 provider 把回答组织为
    内容块列表（[{"type": "text", "text": ...}, ...]），这里同时兼容
    字符串与列表两种形态；无法识别的块类型跳过，不抛错。
    """
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _parse_variants(text: str, max_variants: int) -> list[str]:
    """把模型输出解析成变体列表：逐行清洗 → 去重保序 → 截断到上限。

    清洗规则（按序）：剥编号/项目符号前缀 → 去首尾空白 → 去成对引号 →
    丢弃空行与超长行。全部清洗后为空时返回空列表，由调用方抛错
    （检索层据此降级为原始 query）。
    """
    variants: list[str] = []
    for raw_line in text.splitlines():
        line = _NUMBERING_PREFIX.sub("", raw_line).strip().strip(_VARIANT_WRAPPERS).strip()
        if not line or len(line) > _MAX_VARIANT_CHARS:
            continue
        variants.append(line)
    # dict.fromkeys 去重并保持首次出现顺序（与 retrieval.py 同一惯例）。
    return list(dict.fromkeys(variants))[:max_variants]


class LLMQueryRewriter:
    """用聊天模型把一个查询改写成多个检索变体（QueryRewriter 协议实现）。

    参数（面向初学者）：
    - model：满足 _RewriterModel 协议的聊天模型（生产为独立轻量实例，
      见 api/app.py 装配注释；测试注入替身，零网络）；
    - max_variants：模型产出变体的数量上限（不含自动保留的原查询，
      返回列表总长 ≤ max_variants + 1）；必须为正；
    - cache_size：改写结果缓存容量（LRU，超限弹最旧）；必须为正。

    异常约定：模型调用失败、返回内容无法解析出任何变体时抛错——
    检索层（retrieval._safe_variants）会把任何异常降级为「原始 query
    单路检索」，检索永不因改写失败而阻断（见 retrieval.py 第 5 节）。
    """

    def __init__(
        self,
        model: _RewriterModel,
        *,
        max_variants: int = 3,
        cache_size: int = 128,
    ) -> None:
        if max_variants <= 0:
            raise ValueError("max_variants must be positive")
        if cache_size <= 0:
            raise ValueError("cache_size must be positive")
        self._model = model
        self._max_variants = max_variants
        self._cache_size = cache_size
        self._cache: OrderedDict[str, list[str]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def rewrite(self, query: str) -> list[str]:
        """改写查询，返回「原查询 + 模型变体」的去重列表。

        - 空白 query：原样返回 [query]（不发模型调用——空白查询由索引层
          返回空结果，为它浪费一次模型调用没有意义）；
        - 缓存命中：直接返回缓存列表（拷贝，防调用方就地修改污染缓存）；
        - 模型产出与查询自身重复时去重；原查询永远在首位（契约见模块
          注释第 3 节：改写只增不减，召回下限不退化）。
        """
        if not query.strip():
            return [query]
        key = query.strip()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return list(cached)

        variants = self._generate(key)
        # 原查询在首位 + 去重保序：模型变体中若已含原查询不会重复出现。
        result = list(dict.fromkeys([key, *variants]))
        with self._cache_lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return list(result)

    def _generate(self, query: str) -> list[str]:
        """调用模型产出变体并解析；任何失败都以异常形式上抛。"""
        message = self._model.invoke(
            [
                SystemMessage(content=_REWRITE_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"用户问题：{query}\n"
                        f"请产出至多 {self._max_variants} 个检索变体。"
                    )
                ),
            ]
        )
        variants = _parse_variants(_message_text(message), self._max_variants)
        if not variants:
            raise ValueError("query rewriter model returned no usable variants")
        return variants


__all__ = ["LLMQueryRewriter"]
