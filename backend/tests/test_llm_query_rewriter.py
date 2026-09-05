"""S5 LLM 查询改写器（LLMQueryRewriter）单元测试。

覆盖清单（生产注入组件的全分支覆盖，与既有知识组件测试同一要求）：
1. 正常路径：多行模型输出解析为变体列表；原查询永远在首位（召回
   下限不退化契约）；模型变体去重保序；
2. 解析容错：编号（1. / 2、/ 3)）/ 项目符号（- / *）/ 成对引号 /
   空白行 / 超长行（模型混入解释段落）的清洗；
3. 边界与异常：空白 query 短路（不发模型调用）；模型输出无可解析
   变体 → ValueError；模型调用异常 → 原样上抛（检索层据此降级）；
   max_variants / cache_size 非法值在构造期拒绝；
4. 缓存：同 query 第二次命中缓存（不再调模型）；容量超限弹出最旧
   条目（被弹出的 query 再查会重新调模型）；缓存键忽略首尾空白；
5. 与检索链路集成：注入 KnowledgeService 后，search 实际使用模型
   变体检索到原查询命不中的 chunk（证明接线真实生效）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.llm_rewriter import LLMQueryRewriter
from core.knowledge.models import KnowledgeChunk
from core.knowledge.service import KnowledgeService


class _FakeModel:
    """模型替身：返回预定文本，记录每次调用的消息。"""

    def __init__(self, outputs: list[str] | str, error: Exception | None = None) -> None:
        # 单字符串 = 每次调用返回同一文本；列表 = 按调用次序依次返回。
        self._outputs = [outputs] if isinstance(outputs, str) else list(outputs)
        self._error = error
        self.calls: list[list[BaseMessage]] = []

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        index = min(len(self.calls) - 1, len(self._outputs) - 1)
        return AIMessage(content=self._outputs[index])


def test_rewrite_returns_original_first_then_model_variants() -> None:
    """正常路径：原查询首位 + 模型变体按序追加（召回下限契约）。"""
    model = _FakeModel("过拟合 正则化\n泛化误差 控制\n模型复杂度")
    rewriter = LLMQueryRewriter(model)

    result = rewriter.rewrite("过拟合怎么办")

    assert result == ["过拟合怎么办", "过拟合 正则化", "泛化误差 控制", "模型复杂度"]
    # 模型被调用一次，且收到的是 system + human 两条消息。
    assert len(model.calls) == 1
    assert len(model.calls[0]) == 2


def test_rewrite_strips_numbering_bullets_and_quotes() -> None:
    """解析容错：编号/项目符号/成对引号/空白行都被清洗。

    注意 max_variants=4：模型输出清洗后有 4 个有效变体，默认上限 3
    会截掉最后一个（截断行为另有专测覆盖）。
    """
    rewriter = LLMQueryRewriter(
        _FakeModel(
            '1. "支持向量机 间隔"\n'
            "2、支持向量机 核函数\n"
            "3) 'SVM 最大间隔'\n"
            "- 《SVM 对偶问题》\n"
            "* 支持向量机 间隔\n"  # 与第 1 行清洗后重复 → 去重
            "\n"
            "   \n"
        ),
        max_variants=4,
    )

    result = rewriter.rewrite("什么是支持向量机")

    assert result == [
        "什么是支持向量机",
        "支持向量机 间隔",
        "支持向量机 核函数",
        "SVM 最大间隔",
        "SVM 对偶问题",
    ]


def test_rewrite_drops_overlong_lines() -> None:
    """超长行（模型混入的解释段落）被丢弃，不进入检索变体。"""
    long_paragraph = "这是一段解释" * 50  # 远超 _MAX_VARIANT_CHARS
    rewriter = LLMQueryRewriter(_FakeModel(f"正则化方法\n{long_paragraph}"))

    result = rewriter.rewrite("过拟合怎么办")

    assert result == ["过拟合怎么办", "正则化方法"]


def test_rewrite_truncates_to_max_variants() -> None:
    """模型产出超过 max_variants 时截断（含原查询总长 = max_variants+1）。"""
    rewriter = LLMQueryRewriter(
        _FakeModel("变体一\n变体二\n变体三\n变体四\n变体五"), max_variants=2
    )

    result = rewriter.rewrite("原始问题")

    assert result == ["原始问题", "变体一", "变体二"]


def test_rewrite_deduplicates_model_echo_of_original_query() -> None:
    """模型变体中已含原查询时去重：原查询只出现一次（仍在首位）。"""
    rewriter = LLMQueryRewriter(_FakeModel("过拟合怎么办\n正则化\n过拟合怎么办"))

    result = rewriter.rewrite("过拟合怎么办")

    assert result == ["过拟合怎么办", "正则化"]


def test_rewrite_blank_query_short_circuits_without_model_call() -> None:
    """空白 query 原样返回且不发模型调用（无意义的调用直接省略）。"""
    model = _FakeModel("不应被使用")
    rewriter = LLMQueryRewriter(model)

    assert rewriter.rewrite("   ") == ["   "]
    assert model.calls == []


def test_rewrite_raises_when_no_usable_variant() -> None:
    """模型输出清洗后为空 → ValueError（检索层据此降级为原始 query）。"""
    rewriter = LLMQueryRewriter(_FakeModel("\n\n   \n"))

    with pytest.raises(ValueError, match="no usable variants"):
        rewriter.rewrite("过拟合怎么办")


def test_rewrite_propagates_model_exception() -> None:
    """模型调用异常原样上抛（检索层 _safe_variants 负责降级，不吞错）。"""
    rewriter = LLMQueryRewriter(_FakeModel("", error=RuntimeError("network down")))

    with pytest.raises(RuntimeError, match="network down"):
        rewriter.rewrite("过拟合怎么办")


def test_rewrite_rejects_invalid_constructor_arguments() -> None:
    """构造期校验：max_variants / cache_size 必须为正（尽早失败）。"""
    with pytest.raises(ValueError, match="max_variants"):
        LLMQueryRewriter(_FakeModel("x"), max_variants=0)
    with pytest.raises(ValueError, match="cache_size"):
        LLMQueryRewriter(_FakeModel("x"), cache_size=0)


def test_rewrite_cache_hit_avoids_second_model_call() -> None:
    """缓存命中：同一 query（含首尾空白差异）第二次不再调用模型。"""
    model = _FakeModel("正则化")
    rewriter = LLMQueryRewriter(model)

    first = rewriter.rewrite("过拟合怎么办")
    second = rewriter.rewrite("  过拟合怎么办  ")  # 首尾空白归一到同一缓存键

    assert first == second
    assert len(model.calls) == 1


def test_rewrite_cache_evicts_oldest_entry_beyond_capacity() -> None:
    """LRU 容量：超限弹出最久未使用条目，被弹出的 query 重新调模型。"""
    model = _FakeModel(["变体甲", "变体乙", "变体丙", "变体甲新"])
    rewriter = LLMQueryRewriter(model, cache_size=2)

    rewriter.rewrite("问题一")  # 缓存：{一}
    rewriter.rewrite("问题二")  # 缓存：{一, 二}
    rewriter.rewrite("问题三")  # 容量 2 → 弹出「一」，缓存：{二, 三}
    assert len(model.calls) == 3

    rewriter.rewrite("问题二")  # 命中缓存（未弹出），不调模型
    assert len(model.calls) == 3

    rewriter.rewrite("问题一")  # 「一」已被弹出 → 重新调模型
    assert len(model.calls) == 4


def test_rewrite_cache_returns_copies_against_mutation() -> None:
    """缓存返回拷贝：调用方就地修改返回列表不污染缓存内容。"""
    rewriter = LLMQueryRewriter(_FakeModel("正则化"))

    first = rewriter.rewrite("过拟合怎么办")
    first.append("被调用方篡改")

    assert rewriter.rewrite("过拟合怎么办") == ["过拟合怎么办", "正则化"]


def test_rewrite_handles_list_content_blocks() -> None:
    """模型返回内容块列表（非纯字符串）时拼接其中的文本块。"""

    class _BlockModel:
        def invoke(self, messages: list[BaseMessage]) -> AIMessage:
            return AIMessage(
                content=[
                    {"type": "text", "text": "正则化"},
                    {"type": "reasoning", "reasoning": "忽略我"},
                    "交叉验证",
                ]
            )

    rewriter = LLMQueryRewriter(_BlockModel())

    assert rewriter.rewrite("过拟合怎么办") == ["过拟合怎么办", "正则化", "交叉验证"]


def test_rewrite_integrates_with_knowledge_service_search() -> None:
    """集成：改写变体让原查询命不中的 chunk 被检索到（接线真实生效）。

    词法索引按字符命中打分：原查询「怎样让考试不再死背答案」与 chunk
    「过拟合与正则化：控制模型复杂度」无任一字/词交集 → 零命中；
    模型变体「过拟合 正则化」直接命中 → 多变体联合检索把该 chunk
    召回（max 合并）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            KnowledgeChunk(
                chunk_id="c-regularization",
                document_id="ml",
                content="过拟合与正则化：控制模型复杂度",
                source="ml",
                page=None,
                start=0,
                end=15,
                metadata={},
            )
        ]
    )
    plain = KnowledgeService(index)
    rewritten = KnowledgeService(
        index, rewriter=LLMQueryRewriter(_FakeModel("过拟合 正则化"))
    )

    assert plain.search("怎样让考试不再死背答案", top_k=5) == []
    hits = rewritten.search("怎样让考试不再死背答案", top_k=5)
    assert [hit.chunk.chunk_id for hit in hits] == ["c-regularization"]
