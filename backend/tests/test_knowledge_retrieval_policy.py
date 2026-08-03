"""S4-T3 检索必要性判断测试：RetrievalPolicy 协议与规则式实现。

覆盖清单 A S4-T3 验收标准第 1 条：
1. 简单问题不触发检索：寒暄（中/英）、纯计算、空查询、纯标点、
   过短查询 → needs_retrieval 返回 False，且 reason 说明命中哪条
   规则（判断逻辑可解释、可测试）；
2. 正常问题触发检索：常规提问 → True；
3. 宁多勿漏：含寒暄词的实质问题、代数式、概念性问题 → True
   （规则不误伤正常问题，见 policy.py 模块注释第 5 节）；
4. 默认零回归：AlwaysRetrievalPolicy 总是返回 True（默认策略行为
   与现状一致——总是检索），服务默认 adaptive_search 也总是检索。
"""
from __future__ import annotations

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.policy import (
    AlwaysRetrievalPolicy,
    HeuristicRetrievalPolicy,
    RetrievalDecision,
    RetrievalPolicy,
)
from core.knowledge.service import KnowledgeService

# ── 1. 寒暄/问候 → 不检索 ─────────────────────────────────────────


def test_greeting_chinese_skips_retrieval() -> None:
    """中文寒暄（含标点变体）→ 不检索，理由说明「问候/寒暄」。"""
    policy = HeuristicRetrievalPolicy()
    for query in ("你好", "您好", "谢谢", "再见", "你好！", "嗨"):
        decision = policy.needs_retrieval(query)
        assert decision.retrieve is False, query
        assert "问候" in decision.reason, query


def test_greeting_english_skips_retrieval() -> None:
    """英文寒暄（大小写、带标点变体）→ 不检索。"""
    policy = HeuristicRetrievalPolicy()
    for query in ("hi", "Hello", "HELLO", "thanks", "Thank you", "goodbye!"):
        decision = policy.needs_retrieval(query)
        assert decision.retrieve is False, query
        assert "问候" in decision.reason, query


def test_greeting_with_real_question_still_retrieves() -> None:
    """含寒暄词的实质问题 → 检索（词表是「整句相等」而非包含）。

    若误用包含判断，"你好，什么是SVM" 会被当成寒暄跳过检索——这是
    漏检（该给证据时没给），宁多勿漏（policy.py 模块注释第 5 节
    第 3 条）。
    """
    decision = HeuristicRetrievalPolicy().needs_retrieval("你好，什么是SVM")
    assert decision.retrieve is True
    assert "常规问题" in decision.reason


# ── 2. 纯计算 → 不检索 ────────────────────────────────────────────


def test_pure_calculation_skips_retrieval() -> None:
    """纯计算表达式（含疑问尾缀/符号/尾缀+问号变体）→ 不检索。

    "1+1等于几？" 是「中文疑问尾缀 + 问号」写法（正则允许尾缀后
    再带一个可选问号）；"1+1等于几" 无问号同样命中（Minor-1 回归
    锁定）。
    """
    policy = HeuristicRetrievalPolicy()
    for query in (
        "1+1",
        "1 + 1",
        "2*3=？",
        "2*3=?",
        "100-7等于几",
        "100-7等于几？",
        "1+1等于几",
        "3.14×2",
        "50%",
        "100-7",
    ):
        decision = policy.needs_retrieval(query)
        assert decision.retrieve is False, query
        assert "纯计算" in decision.reason, query


def test_calculation_like_concept_question_retrieves() -> None:
    """「计算 + 概念」问题 → 检索（不是纯计算，规则不误伤）。

    "1+1 为什么等于2" 是概念性问题（进位/加法原理），检索合理；
    "a+b" 是代数式（含字母），不是数值计算，同样检索。
    """
    policy = HeuristicRetrievalPolicy()
    for query in ("1+1 为什么等于2", "a+b", "什么是质数"):
        assert policy.needs_retrieval(query).retrieve is True, query


# ── 3. 空查询 / 无实质内容 / 过短 → 不检索 ───────────────────────


def test_blank_and_punctuation_only_skip_retrieval() -> None:
    """空查询、纯标点 → 不检索（没有任何可查的词/数）。"""
    policy = HeuristicRetrievalPolicy()
    for query in ("   ", "？？？", "!!!", "……"):
        decision = policy.needs_retrieval(query)
        assert decision.retrieve is False, repr(query)
        assert decision.reason  # 理由非空（可解释）


def test_too_short_query_skips_retrieval() -> None:
    """单字符查询（"嗯"/"好"）→ 不检索（过短规则）。"""
    for query in ("嗯", "好", "是"):
        decision = HeuristicRetrievalPolicy().needs_retrieval(query)
        assert decision.retrieve is False, query
        assert "过短" in decision.reason, query


def test_short_abbreviation_still_retrieves() -> None:
    """2 字符缩写（"AI"）→ 检索（宁多勿漏，阈值是 1 而非更大）。

    "AI"、"SVM" 这类缩写是常见合法查询，过短规则只拦 1 字符
    （policy.py 模块注释第 5 节第 5 条）。
    """
    policy = HeuristicRetrievalPolicy()
    assert policy.needs_retrieval("AI").retrieve is True
    assert policy.needs_retrieval("SVM").retrieve is True


# ── 4. 正常问题 → 检索 ───────────────────────────────────────────


def test_normal_question_retrieves() -> None:
    """常规提问 → 检索，理由说明「常规问题：需要检索」。"""
    decision = HeuristicRetrievalPolicy().needs_retrieval("什么是支持向量机")
    assert decision.retrieve is True
    assert "常规问题" in decision.reason


# ── 5. 默认零回归：总是检索 ──────────────────────────────────────


def test_always_policy_always_retrieves() -> None:
    """AlwaysRetrievalPolicy 对任何查询（含寒暄）都返回 True。"""
    policy = AlwaysRetrievalPolicy()
    for query in ("你好", "1+1", "什么是SVM", "   "):
        decision = policy.needs_retrieval(query)
        assert decision.retrieve is True
        assert decision.reason == "默认策略：总是检索"


def test_always_policy_satisfies_protocol() -> None:
    """两个实现都满足 RetrievalPolicy 协议（鸭子类型，可注入）。"""
    for policy in (AlwaysRetrievalPolicy(), HeuristicRetrievalPolicy()):
        decision: RetrievalDecision = policy.needs_retrieval("SVM")
        assert isinstance(decision, RetrievalDecision)
    # 显式标注协议类型也能通过（mypy 静态检查点）。
    injected: RetrievalPolicy = HeuristicRetrievalPolicy()
    assert injected.needs_retrieval("SVM").retrieve is True


def test_service_default_adaptive_search_always_retrieves() -> None:
    """服务默认 adaptive_search：不注入策略时总是检索（零回归）。

    即便 query 是寒暄（HeuristicRetrievalPolicy 会跳过），默认
    服务仍触发检索——默认语义与现状一致（总是检索），需要必要性
    判断的调用方显式注入策略。
    """
    service = KnowledgeService(InMemoryKnowledgeIndex())
    result = service.adaptive_search("你好", top_k=5)
    assert result.metadata.needed is True
    assert result.metadata.need_reason == "默认策略：总是检索"
