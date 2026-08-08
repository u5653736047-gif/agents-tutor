"""把知识检索服务封装为 Agent 可调用的工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator

from .policy import RetrievalPolicy
from .retrieval import QueryRefiner
from .service import KnowledgeService


class _SearchKnowledgeInput(BaseModel):
    """Validate tool inputs before execution so errors are classified correctly."""

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value


# ── S4-T3 阈值未达标提示（Observation 可见文本）──
# 为什么要有这句提示（对齐 S4-T3 验收口径）：检索未达相关性阈值时，
# 结果虽然照常返回，但证据相关性不足——Agent 不应把结果当证据强行
# 注入答案，而应如实说明「知识库可能未覆盖该问题」。工具把这句话
# 直接写进 Observation 文本（模型可见），让「如实说明」有可执行的
# 依据；同时它也是审计线索（评价 Agent 可核对 Agent 是否如实说明）。
_THRESHOLD_MISS_HINT = "知识库检索未达相关性阈值，知识库可能未覆盖该问题"


def create_search_knowledge_tool(
    service: KnowledgeService,
    *,
    policy: RetrievalPolicy | None = None,
    relevance_threshold: float | None = None,
    refiner: QueryRefiner | None = None,
) -> BaseTool:
    """为指定服务创建工具，避免使用全局知识库实例。

    S4-T3 可选接入（面向初学者）：policy / relevance_threshold /
    refiner 是「自适应检索」的装配参数，由装配方（api 层或测试）
    注入：
    - policy：检索必要性策略——寒暄、纯计算等简单问题判定为
      「不需要检索」，直接作答（语义见 policy.py 模块注释）；
    - relevance_threshold：相关性阈值——本轮最高分低于它 →
      未达标，Agent 应说明「知识库未覆盖」而非强行作答；
    - refiner：查询精化器——未达标时换一个新查询重检（次数上限
      由 service 构造时的 max_refine_rounds 决定）。
    默认都不注入 → 工具走原 service.search() 路径，输出与接入前
    逐项一致（零回归，也不产生检索决策事件）；任一注入 → 走
    service.adaptive_search()，输出 JSON 附带检索元数据（rounds /
    threshold_met / stopped_reason / hit_count / top_score / needed
    / need_reason），供 core 侧（graph_builder）转成运行事件。

    零耦合方向（重要设计）：knowledge 包刻意不依赖 core/events.py
    （详见 retrieval.py 模块注释第 8 节第 4 点）——本模块不 import
    events、不 emit，检索元数据只是纯 JSON 结构随工具结果返回；由
    core 侧（graph_builder._wrap）解析后转成事件。方向是
    knowledge →（JSON）→ core 转换 → events，knowledge 永远不知道
    事件体系的存在。
    """

    # 零回归判定：三个装配参数全部未注入 → 自适应未启用，工具行为
    # 与接入前完全一致（连输出格式都不变；graph_builder 解析不到
    # metadata，自然不会发事件）。「显式注入才启用」与 retrieval.py
    # 的默认语义（policy=None / threshold=None / refiner=None → 退化
    # 为单轮检索）是同一哲学。
    adaptive_enabled = (
        policy is not None
        or relevance_threshold is not None
        or refiner is not None
    )

    @tool("search_knowledge", args_schema=_SearchKnowledgeInput)
    def search_knowledge(query: str, top_k: int = 5) -> dict[str, Any]:
        """检索可引用的知识片段。"""
        if not adaptive_enabled:
            # 未启用自适应：原路径原样保留（零回归，见上方注释）——
            # 输出不含 metadata 键，旧消费者（引用收集、评价证据、
            # 事件转换）全部无感。
            hits = service.search(query, top_k)
            if not hits:
                return {
                    "found": False,
                    "message": "未找到可引用的知识片段",
                    "hits": [],
                }
            return {
                "found": True,
                "hits": [
                    {
                        "content": hit.chunk.content,
                        "score": hit.score,
                        "citation": hit.citation.model_dump(mode="json"),
                    }
                    for hit in hits
                ],
            }
        # 启用自适应：注入值覆盖 service 构造时配置（覆盖语义见
        # service.adaptive_search 的参数说明——None 沿用构造时配置，
        # 非 None 用工具装配方的注入值）。
        result = service.adaptive_search(
            query,
            top_k,
            policy=policy,
            relevance_threshold=relevance_threshold,
            refiner=refiner,
        )
        meta = result.metadata
        # metadata 字段语义（面向初学者）：
        # - needed：本次是否需要检索（False = 简单问题直接作答，
        #   hits 为空）；
        # - need_reason：必要性判定理由（中文，可解释）；
        # - threshold_met：最终是否达到相关性阈值（None = 未启用
        #   阈值判定；False = 证据相关性不足，见 _THRESHOLD_MISS_HINT）；
        # - stopped_reason：检索/重检停止原因（中文，转事件用）；
        # - rounds：检索轮数（首轮 + 重检次数，未检索时为 0）；
        # - hit_count / top_score：最终一轮的命中数与最高分（决策
        #   依据，与 hits 一一对应）。
        # 刻意不输出每轮 query（查询正文）：查询已在工具调用参数与
        # 工具结果审计中，元数据与事件再记一遍就是双重存储 + 敏感
        # 正文外泄（事件载荷同理，见 events.py 的 retrieval_* 注释）。
        last_round = meta.rounds[-1] if meta.rounds else None  # 末轮即最终决策依据轮
        payload: dict[str, Any] = {
            "found": bool(result.hits),
            "hits": [
                {
                    "content": hit.chunk.content,
                    "score": hit.score,
                    "citation": hit.citation.model_dump(mode="json"),
                }
                for hit in result.hits
            ],
            "metadata": {
                "needed": meta.needed,
                "need_reason": meta.need_reason,
                "threshold_met": meta.threshold_met,
                "stopped_reason": meta.stopped_reason,
                "rounds": len(meta.rounds),
                "hit_count": last_round.hit_count if last_round else 0,
                "top_score": last_round.top_score if last_round else 0.0,
            },
        }
        if not result.hits:
            payload["message"] = "未找到可引用的知识片段"
        if meta.threshold_met is False:
            # 阈值未达标：Observation 明确提示，Agent 应如实说明
            # 「知识库未覆盖」而非强行作答（见 _THRESHOLD_MISS_HINT
            # 注释）。
            payload["hint"] = _THRESHOLD_MISS_HINT
        return payload

    return search_knowledge


__all__ = ["create_search_knowledge_tool"]
