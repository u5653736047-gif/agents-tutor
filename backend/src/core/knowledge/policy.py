"""S4-T3 自适应 RAG（一）：检索必要性判断（RetrievalPolicy）。

（面向初学者的设计说明，按功能模块）

1. 这个模块解决什么问题
   不是每个用户问题都需要查知识库。寒暄（"你好"）、纯计算
   （"1+1 等于几"）这类问题直接作答即可——检索是浪费，还可能把
   无关片段当成证据注入答案。本模块提供「要不要检索」的判定：
   判定结论（RetrievalDecision）包含两个字段：
   - retrieve：要不要检索（True = 要）；
   - reason：为什么（中文说明）——「判定可解释」靠的就是它：
     每一条规则都写明命中理由，上层 Agent 可以直接引用、展示给
     用户，测试也可以精确断言。

2. 三个组件（协议 + 两个实现）
   - RetrievalPolicy（协议）：只约定 needs_retrieval(query) 方法，
     与 QueryRewriter / Reranker 同一注入风格（鸭子类型，可替换）；
   - AlwaysRetrievalPolicy（默认实现）：总是返回「需要检索」——
     这是零回归的默认值（见第 3 节）；
   - HeuristicRetrievalPolicy（规则实现）：自包含启发式规则，不依赖
     Agent 意图（knowledge 模块不反向依赖 Agent 层，见第 4 节）。

3. 为什么默认是「总是检索」（零回归）
   S4-T1 / S4-T2 及更早的行为是「每次 search 都检索」。默认策略
   必须是 AlwaysRetrievalPolicy：不注入策略时，自适应检索退化为
   普通检索，行为与现状逐项一致（既有测试零回归）。需要必要性
   判断的调用方显式注入 HeuristicRetrievalPolicy 或其他实现。

4. 为什么规则自包含、不依赖 Agent 意图
   意图识别（Supervisor）在 Agent 层，knowledge 是下层组件。若
   必要性判断依赖意图，knowledge 就要反向依赖 Agent 层（或被迫
   接收意图注入），把「意图分类」与「检索正确性」两类关注点耦合。
   因此 HeuristicRetrievalPolicy 只用查询文本本身的形状做判断，
   规则可独立测试、可解释。

5. 规则集（按顺序判定，命中即返回；都不命中 → 需要检索）
   (1) 空查询：strip 后为空 → 不检索。索引层对空 query 本来就返回
       空结果，直接短路，避免无意义检索；
   (2) 无实质内容：strip 后不含任何字母/数字/汉字（纯标点、纯符号，
       如 "？？？"）→ 不检索。没有任何词可查，检索必然空手而归；
   (3) 寒暄/问候：整句（去首尾空白与标点、转小写后）等于寒暄词表
       中的词 → 不检索。注意是「整句相等」而不是「包含」：
       "你好，什么是SVM" 含寒暄词但仍是实质问题，照常检索。
       为什么宁多勿漏：检索没有副作用（最多浪费一点时间），漏检
       才是真正的错误（该给证据时没给）——所有规则都遵循这个原则；
   (4) 纯计算：剥掉常见疑问尾缀（等于多少/等于几/是多少/是几/等于/
       =? 等，尾缀后允许再带一个问号）后，剩余部分只由数字与四则
       运算符（+ - * / × ÷）、括号、百分号、小数点组成，且至少含
       一个数字 → 不检索。
       例："1+1"、"2*3=？"、"100-7等于几"、"1+1等于几？" 都命中；
       "1+1 为什么等于2" 因含中文不命中——那已经是概念性问题
       （进位、加法原理），检索是合理的。
       取舍与风险："100-7"、"3.14" 这类纯数字/纯符号查询也会被
       跳过检索（词法索引对纯数字可分词命中，理论上可能有知识库
       内容可查）。这是「宁多勿漏」方向下接受的有界漏检：纯数字
       查询的知识价值低、直接计算即可作答，且上层可用相关性阈值
       兜底——规则宁可保守，不为这类查询破坏规则的简单可解释性；
   (5) 过短：strip 后不超过 1 个字符（如 "嗯"、"好"）→ 不检索。
       为什么阈值是 1 而不是更大：2 字符的 "AI" 等缩写是常见合法
       查询；1 字符几乎不可能构成实质查询。宁多勿漏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# 寒暄/问候词表（全小写、无首尾标点；中文本身无大小写）。
# 为什么用词表而不是「包含判断」：见模块注释第 5 节第 3 条（宁多勿漏）。
_GREETINGS = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "早上好",
        "下午好",
        "晚上好",
        "谢谢",
        "感谢",
        "再见",
        "拜拜",
        "hi",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "thanks",
        "thank you",
        "bye",
        "goodbye",
    }
)

# 纯计算表达式的常见疑问尾缀（从末尾剥掉后检查剩余部分）。
# 顺序无关紧要（正则 alternation 从左到右尝试，$ 锚定末尾）；
# 单独列出 [=?？] 是为了处理 "1+1=?"、"1+1？" 这类带问号的写法。
# 尾缀之后允许再带一个可选问号（\s*[?？]?），覆盖 "1+1等于几？"
# 这类「中文疑问尾缀 + 问号」的写法；不带问号（"1+1等于几"）同样
# 命中。注意不能误伤 "1+1 为什么等于2"：其末尾 "等于2" 不满足
# 尾缀分支（"等于" 后跟数字而非空白/问号/结尾），不会命中。
_CALC_SUFFIX = re.compile(
    r"(等于多少|等于几|是多少|是几|结果是多少|等于|=\s*[?？]?|[?？])\s*[?？]?\s*$"
)

# 纯计算表达式允许出现的字符：数字、四则运算符、括号、百分号、小数点
# 与空白（含全角空格 "1 + 1" 的写法）。其余任何字符（字母、汉字、
# 标点）都会让表达式「不纯」→ 不命中规则（检索更安全）。
_CALC_CHARS = frozenset("0123456789+-*/×÷().%,=． ，,　 ")


@dataclass(frozen=True)
class RetrievalDecision:
    """检索必要性判定结论（可解释）：要不要检索 + 为什么。

    两个字段（面向初学者）：
    - retrieve：True = 需要检索，False = 不需要（直接作答）；
    - reason：判定理由的中文说明——规则写明 + 返回判定原因，
      正是「判断逻辑可解释」的载体，上层 Agent 可直接引用。
    """

    retrieve: bool
    reason: str


class RetrievalPolicy(Protocol):
    """检索必要性策略协议：判定一个 query 是否需要触发检索。

    实现方只需提供 needs_retrieval(query) -> RetrievalDecision 方法
    （鸭子类型，与 QueryRewriter / Reranker 同一注入风格，见
    模块注释第 2 节）。返回的 RetrievalDecision 同时给出结论与理由。
    """

    def needs_retrieval(self, query: str) -> RetrievalDecision:
        """判定 query 是否需要检索，返回结论 + 理由。"""
        ...


class AlwaysRetrievalPolicy:
    """默认策略：总是需要检索（零回归，见模块注释第 3 节）。

    为什么需要它：让「不启用必要性判断」也走策略协议，调用方不需要
    判空逻辑；行为与 S4-T2 及更早完全一致（每次 search 都检索）。
    """

    def needs_retrieval(self, query: str) -> RetrievalDecision:
        return RetrievalDecision(True, "默认策略：总是检索")


class HeuristicRetrievalPolicy:
    """规则式必要性判断：自包含启发式（规则与理由见模块注释第 5 节）。

    规则按顺序判定：空查询 → 无实质内容 → 寒暄/问候 → 纯计算 →
    过短 → 默认需要检索。每条规则命中都返回具体的中文理由，
    测试可以精确断言「哪条规则生效、为什么」。
    """

    def needs_retrieval(self, query: str) -> RetrievalDecision:
        stripped = query.strip()
        # 规则 1：空查询（strip 后为空）→ 不检索。
        # 索引层对空 query 本来就返回空结果，直接短路（见模块注释
        # 第 5 节第 1 条）。
        if not stripped:
            return RetrievalDecision(False, "空查询：没有可检索的内容")
        # 规则 2：无实质内容（不含任何字母/数字/汉字，如 "？？？"）。
        # isalnum 对汉字也返回 True，因此这里判断的是「没有任何可查
        # 的词/数」——检索必然空手而归（见模块注释第 5 节第 2 条）。
        if not any(ch.isalnum() for ch in stripped):
            return RetrievalDecision(
                False, f"无实质内容（仅标点/符号）：'{stripped}'，无需检索"
            )
        # 规则 3：寒暄/问候（整句相等，不是包含——见模块注释第 5 节
        # 第 3 条）。规范化：去首尾空白与常见标点、转小写，再查词表。
        normalized = stripped.strip(" \t！!？?。.，,、；;：:～~…").lower()
        if normalized in _GREETINGS:
            return RetrievalDecision(
                False, f"问候/寒暄用语：'{stripped}'，无需检索"
            )
        # 规则 4：纯计算表达式（剥疑问尾缀后只含数字与四则运算符）。
        if _is_pure_calculation(stripped):
            return RetrievalDecision(
                False, f"纯计算表达式：'{stripped}'，无需检索"
            )
        # 规则 5：过短（strip 后不超过 1 个字符，如 "嗯"、"好"）。
        # 为什么阈值是 1：见模块注释第 5 节第 5 条（宁多勿漏）。
        if len(stripped) <= 1:
            return RetrievalDecision(
                False, f"查询过短（{len(stripped)} 字符）：'{stripped}'，无需检索"
            )
        # 都不命中 → 常规问题，需要检索。
        return RetrievalDecision(True, "常规问题：需要检索")


def _is_pure_calculation(text: str) -> bool:
    """规则 4 的判定：剥掉疑问尾缀后是否「只含数字与四则运算符」。

    两步（面向初学者）：
    1. 用 _CALC_SUFFIX 从末尾剥掉常见疑问尾缀（等于几/是多少/=? 等），
       如 "100-7等于几" → "100-7"、"2*3=？" → "2*3"；
    2. 剩余部分必须「非空、至少含一个数字、且每个字符都在
       _CALC_CHARS（数字/四则运算符/括号/百分号/小数点/空白）里」。
       例："1+1" 命中；"1+1 为什么等于2" 含汉字不命中；"a+b" 含字母
       不命中（代数式是概念性问题，检索合理）。
    """
    stripped = _CALC_SUFFIX.sub("", text).strip()
    if not stripped or not any(ch.isdigit() for ch in stripped):
        return False
    return all(ch in _CALC_CHARS for ch in stripped)


__all__ = [
    "AlwaysRetrievalPolicy",
    "HeuristicRetrievalPolicy",
    "RetrievalDecision",
    "RetrievalPolicy",
]
