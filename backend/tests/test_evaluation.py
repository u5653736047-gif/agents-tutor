"""S2-T3 评价 Agent 基础评价规则测试。

覆盖验收标准：
1. 评价 Agent 对一轮最终回答输出结构化评价：事实准确性、引用完整性
   两个维度 + 通过/存疑/不通过结论 + 理由（EvaluationVerdict /
   EvaluationDimension / EvaluationResult）；
2. 评价输入为最终回答 + 本轮检索证据：证据工具名由核心层组装进
   EvaluationResult.evidence_tool_names（不记正文），模型通过 ReAct
   工具观察拿到检索证据（不凭空评价）；
3. 评价结论写入 state["evaluation"]（checkpoint 持久化、每轮重置）与
   EVALUATION_COMPLETED 事件（只带 verdict 摘要，脱敏不记 reason 正文）；
4. 事实错误回答被判存疑/不通过、引用缺失被标记、正确回答通过；
5. 触发时机：evaluator 的 prompt + submit_evaluation 工具约定
   （对齐 S2-T1/T2 的 detect_intent/detect_level 模式），在 evaluator
   ReAct 轮内产出结构化评价，不新增图节点。

全部使用确定性替身模型（ScriptedModel），不依赖真实模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from core.events import ErrorCode, EventType
from core.graph_builder import CollaborativeAgentGraph
from core.nodes.prompts import ROLE_PROMPTS
from core.state import (
    AgentRole,
    EvaluationDimension,
    EvaluationResult,
    EvaluationVerdict,
    Intent,
)


class ScriptedModel:
    """按图执行顺序返回预设消息，并记录各角色可见上下文。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []
        self.bound_tool_names: list[str] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        self.bound_tool_names = [str(getattr(tool, "name", "")) for tool in tools]
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


# ── 替身响应构造 ───────────────────────────────────────────


def _intent_response(intent: str) -> AIMessage:
    """模型调用 detect_intent 工具并自报意图。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "detect_intent",
                "args": {"intent": intent, "reason": ""},
                "id": f"intent-{intent}",
                "type": "tool_call",
            }
        ],
    )


def _handoff_response(target: str) -> AIMessage:
    """模型调用 handoff 工具请求分派到指定 Worker。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": target},
                "id": f"handoff-{target}",
                "type": "tool_call",
            }
        ],
    )


def _submit_response(
    verdict: str,
    fact_accuracy: str,
    citation_completeness: str,
    reason: str = "",
) -> AIMessage:
    """模型调用 submit_evaluation 工具提交结构化评价。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_evaluation",
                "args": {
                    "verdict": verdict,
                    "fact_accuracy": fact_accuracy,
                    "citation_completeness": citation_completeness,
                    "reason": reason,
                },
                "id": "submit-evaluation",
                "type": "tool_call",
            }
        ],
    )


def _retrieve_response() -> AIMessage:
    """模型调用业务检索工具获取证据（模拟 S2-T4 的检索证据来源）。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "retrieve",
                "args": {"query": "梯度下降"},
                "id": "retrieve-1",
                "type": "tool_call",
            }
        ],
    )


@tool
def retrieve(query: str) -> str:
    """检索知识片段（测试替身业务工具）。"""
    return f"检索到「{query}」相关片段：负梯度方向迭代更新参数。"


def _evaluation_script(
    verdict: str,
    fact_accuracy: str,
    citation_completeness: str,
    reason: str = "",
    *,
    with_retrieve: bool = False,
) -> list[AIMessage]:
    """构造「评价意图 → evaluator 结构化评价」的完整响应脚本。

    响应数量与 ReAct invoke 次数严格一一对应（缺一条就会在最后一次
    invoke 时 responses 为空，被当作 MODEL_CALL_FAILED）：
    - supervisor 轮：detect_intent → handoff → 无工具收尾回答；
    - evaluator 轮：可选 retrieve（检索证据）→ submit_evaluation →
      无工具收尾回答；
    - supervisor 聚合轮：最终汇总（无工具）。
    """
    responses: list[AIMessage] = [
        _intent_response("evaluation"),
        _handoff_response("evaluator"),
        AIMessage(content="任务已分派"),
    ]
    if with_retrieve:
        responses.append(_retrieve_response())
    responses.extend(
        [
            _submit_response(
                verdict, fact_accuracy, citation_completeness, reason
            ),
            AIMessage(content="评价完成。"),
            AIMessage(content="最终汇总"),
        ]
    )
    return responses


def _evaluation_events(result: dict, *, after_sequence: int = -1) -> list:
    """过滤出 EVALUATION_COMPLETED 事件。

    after_sequence：只统计 sequence 大于该值的事件。run() 返回的 events
    是跨轮累积（events 通道按 operator.add 追加，checkpoint 全量保留），
    因此多轮会话断言「本轮新增」时必须按 sequence 差分——与 api 层
    _public_events 的消费方式一致。
    """
    return [
        event
        for event in result["events"]
        if event.event_type is EventType.EVALUATION_COMPLETED
        and event.sequence > after_sequence
    ]


def _switched_agents(result: dict) -> list[str]:
    """按顺序列出 AGENT_SWITCHED 事件的目标。"""
    return [
        event.agent
        for event in result["events"]
        if event.event_type is EventType.AGENT_SWITCHED
    ]


# ── 枚举与工具契约 ─────────────────────────────────────────


def test_evaluation_enums_cover_required_categories() -> None:
    """验收：结论枚举含通过/存疑/不通过，维度枚举含事实准确性与引用完整性。"""
    assert {verdict.value for verdict in EvaluationVerdict} == {
        "pass",
        "questionable",
        "fail",
    }
    assert {dimension.value for dimension in EvaluationDimension} == {
        "fact_accuracy",
        "citation_completeness",
    }


def test_evaluator_prompt_defines_evaluation_contract() -> None:
    """验收：评价规则写在 evaluator 提示词中（工具约定 + 双维度 + 禁止凭空评价）。"""
    prompt = ROLE_PROMPTS[AgentRole.EVALUATOR]
    assert "评价助手" in prompt  # 既有锚点（test_graph_builder 依赖该词）
    assert "submit_evaluation" in prompt
    assert "fact_accuracy" in prompt
    assert "citation_completeness" in prompt
    assert "禁止凭空评价" in prompt  # 必须基于最终回答与检索证据，不凭空评价
    assert "检索证据" in prompt


def test_submit_evaluation_tool_is_evaluator_only() -> None:
    """submit_evaluation 注册且仅 evaluator 可调用，schema 含三结论枚举。"""
    graph = CollaborativeAgentGraph(model=ScriptedModel([]))
    tool = graph.registry.get("submit_evaluation")

    assert tool is not None
    assert tool.args_schema is not None
    schema_text = str(tool.args_schema.model_json_schema())
    assert "pass" in schema_text
    assert "questionable" in schema_text
    assert "fail" in schema_text
    assert graph.registry.is_authorized("submit_evaluation", AgentRole.EVALUATOR)
    assert not graph.registry.is_authorized(
        "submit_evaluation", AgentRole.SUPERVISOR
    )
    assert not graph.registry.is_authorized(
        "submit_evaluation", AgentRole.TEACHING_ASSISTANT
    )
    assert not graph.registry.is_authorized(
        "submit_evaluation", AgentRole.LEARNING_ASSISTANT
    )
    bound = cast(ScriptedModel, graph.agents[AgentRole.EVALUATOR].model)
    assert "submit_evaluation" in bound.bound_tool_names


# ── 结构化评价写入 state 与事件 ─────────────────────────────


def test_evaluation_written_to_state_and_event() -> None:
    """验收：评价结论写入 state["evaluation"] 与 EVALUATION_COMPLETED 事件。"""
    model = ScriptedModel(
        _evaluation_script("pass", "pass", "pass", "回答准确且引用完整")
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请评价这段回答：梯度下降是迭代优化算法", "eval-state-event")

    assert result["intent"] == Intent.EVALUATION
    assert _switched_agents(result) == ["evaluator", "supervisor"]
    assert result["run_error"] is None
    evaluation = result["evaluation"]
    assert isinstance(evaluation, EvaluationResult)
    assert evaluation.verdict == EvaluationVerdict.PASS
    assert evaluation.fact_accuracy == EvaluationVerdict.PASS
    assert evaluation.citation_completeness == EvaluationVerdict.PASS
    assert evaluation.reason == "回答准确且引用完整"
    assert evaluation.evidence_tool_names == []  # 本轮无检索证据
    eval_events = _evaluation_events(result)
    assert len(eval_events) == 1
    assert eval_events[0].agent == AgentRole.EVALUATOR.value
    assert eval_events[0].evaluation_verdict == EvaluationVerdict.PASS.value
    assert eval_events[0].success is True


def test_evaluation_event_redacts_reason() -> None:
    """脱敏：事件只记 verdict 摘要，不记录 reason 等敏感正文；完整结论在 state。"""
    model = ScriptedModel(
        _evaluation_script("fail", "fail", "fail", "回答包含学生作文细节……")
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请评价这篇作文", "eval-redaction")

    # 完整结论（含 reason）在 state，供审计读取
    assert result["evaluation"] is not None
    assert result["evaluation"].reason == "回答包含学生作文细节……"
    eval_events = _evaluation_events(result)
    assert len(eval_events) == 1
    # RunEvent 模型本身没有 reason/content 字段（事件协议无敏感正文），
    # 只有 evaluation_verdict 摘要；用 model_dump 证明字段集合不含正文
    dumped = eval_events[0].model_dump()
    assert "reason" not in dumped
    assert "content" not in dumped
    assert dumped["evaluation_verdict"] == "fail"


# ── 三类结论场景（验收核心） ────────────────────────────────


def test_correct_answer_passes() -> None:
    """正确回答：事实准确、引用完整 → 总结论通过（pass）。"""
    model = ScriptedModel(
        _evaluation_script(
            "pass",
            "pass",
            "pass",
            "与检索证据一致，回答准确且引用了证据",
            with_retrieve=True,
        )
    )
    graph = CollaborativeAgentGraph(
        model=model,
        tools=[retrieve],
        tool_permissions={"retrieve": {AgentRole.EVALUATOR}},
    )

    result = graph.run("请评价这段回答", "eval-correct")

    evaluation = result["evaluation"]
    assert evaluation is not None
    assert evaluation.verdict == EvaluationVerdict.PASS
    assert evaluation.fact_accuracy == EvaluationVerdict.PASS
    assert evaluation.citation_completeness == EvaluationVerdict.PASS


def test_factual_error_answer_fails() -> None:
    """事实错误回答：事实准确性不通过 → 总结论不通过（fail）。"""
    model = ScriptedModel(
        _evaluation_script("fail", "fail", "pass", "回答与检索证据矛盾")
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请评价这段回答", "eval-factual-error")

    evaluation = result["evaluation"]
    assert evaluation is not None
    assert evaluation.verdict == EvaluationVerdict.FAIL
    assert evaluation.fact_accuracy == EvaluationVerdict.FAIL
    assert _evaluation_events(result)[0].evaluation_verdict == "fail"


def test_factual_uncertainty_flagged_questionable() -> None:
    """事实存疑回答：被判存疑（questionable），而非整体否定。"""
    model = ScriptedModel(
        _evaluation_script(
            "questionable", "questionable", "pass", "个别表述不够准确，需复核"
        )
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请评价这段回答", "eval-questionable")

    evaluation = result["evaluation"]
    assert evaluation is not None
    assert evaluation.verdict == EvaluationVerdict.QUESTIONABLE
    assert evaluation.fact_accuracy == EvaluationVerdict.QUESTIONABLE
    assert _evaluation_events(result)[0].evaluation_verdict == "questionable"


def test_missing_citation_flagged() -> None:
    """引用缺失被标记：无检索证据时引用完整性维度判不通过，总结论存疑。"""
    model = ScriptedModel(
        _evaluation_script(
            "questionable", "pass", "fail", "回答正确但未引用检索证据"
        )
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请评价这段回答", "eval-missing-citation")

    evaluation = result["evaluation"]
    assert evaluation is not None
    assert evaluation.citation_completeness == EvaluationVerdict.FAIL
    assert evaluation.verdict == EvaluationVerdict.QUESTIONABLE
    # 本轮未调用任何检索工具 → 证据工具名列表为空（核心层组装）
    assert evaluation.evidence_tool_names == []


# ── 评价输入组装：检索证据 ──────────────────────────────────


def test_citation_evidence_recorded() -> None:
    """带检索证据：证据工具名写入评价结果（核心层组装，不记正文）。"""
    model = ScriptedModel(
        _evaluation_script(
            "pass",
            "pass",
            "pass",
            "依据检索证据，回答准确且引用完整",
            with_retrieve=True,
        )
    )
    graph = CollaborativeAgentGraph(
        model=model,
        tools=[retrieve],
        tool_permissions={"retrieve": {AgentRole.EVALUATOR}},
    )

    result = graph.run("请评价这段回答", "eval-with-evidence")

    evaluation = result["evaluation"]
    assert evaluation is not None
    assert evaluation.verdict == EvaluationVerdict.PASS
    assert evaluation.citation_completeness == EvaluationVerdict.PASS
    # 证据工具名由核心层从本轮 ToolResult 组装（不是模型填写的）
    assert evaluation.evidence_tool_names == ["retrieve"]
    # 证据正文不复制进评价模型（正文仍在 state["tool_results"] 按工具结果
    # 审计），评价模型只记工具名，避免双重存储与正文扩散
    assert "负梯度方向" not in evaluation.reason
    # tool_results 是跨轮累积（supervisor 的 detect_intent/handoff +
    # evaluator 的 retrieve/submit_evaluation）：检索工具结果确实进入了
    # 本轮工具结果审计，且先于评价工具调用
    tool_names = [item.tool_name for item in result["tool_results"]]
    assert tool_names.count("retrieve") == 1
    assert tool_names.count("submit_evaluation") == 1
    assert tool_names.index("retrieve") < tool_names.index("submit_evaluation")


# ── 生命周期：持久化与每轮重置 ──────────────────────────────


def test_evaluation_persists_in_checkpoint_and_resets_per_turn() -> None:
    """评价随 checkpoint 持久化（get_state 可读），新轮重置为 None。"""
    model = ScriptedModel(
        [
            *_evaluation_script("pass", "pass", "pass", "第一轮评价"),
            # 第二轮：无评价意图，直接回答
            AIMessage(content="第二轮的普通回答"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())
    session_id = "eval-lifecycle"
    user_id = "user-1"

    first = graph.run("请评价这段回答", session_id, user_id)

    assert first["evaluation"] is not None
    assert first["evaluation"].verdict == EvaluationVerdict.PASS
    persisted = graph.get_state(session_id, user_id)
    assert persisted is not None
    # checkpoint 反序列化后 evaluation 通道可能是 dict 或 EvaluationResult
    # 实例（视序列化器而定），统一归一为模型再断言，两种形式都兼容
    persisted_raw = persisted["evaluation"]
    if isinstance(persisted_raw, EvaluationResult):
        persisted_raw = persisted_raw.model_dump()
    persisted_eval = EvaluationResult.model_validate(persisted_raw)
    assert persisted_eval.verdict == EvaluationVerdict.PASS
    assert persisted_eval.reason == "第一轮评价"

    second = graph.run("你好", session_id, user_id)

    # 新轮开始 evaluation 被重置为 None（与 intent 同构：评价是本轮结论）；
    # run() 返回的 events 是跨轮累积（含第一轮遗留的 EVALUATION_COMPLETED），
    # 因此断言「本轮新增」需按 sequence 差分
    assert second["evaluation"] is None
    assert second["messages"][-1].content == "第二轮的普通回答"
    first_last_sequence = max(event.sequence for event in first["events"])
    assert len(_evaluation_events(second)) == 1  # 第一轮遗留
    assert _evaluation_events(second, after_sequence=first_last_sequence) == []


# ── 写入端严格性与审计有界性 ────────────────────────────────


def test_invalid_verdict_rejected_without_crash() -> None:
    """写入端严格：非法评价值被工具层拒绝，运行宽容降级为无评价。"""
    model = ScriptedModel(
        [
            _intent_response("evaluation"),
            _handoff_response("evaluator"),
            AIMessage(content="任务已分派"),
            _submit_response("not_a_verdict", "pass", "pass"),
            AIMessage(content="评价完成。"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请评价这段回答", "eval-invalid")

    assert result["evaluation"] is None
    assert _evaluation_events(result) == []
    failed = [
        item
        for item in result["tool_results"]
        if item.tool_name == "submit_evaluation"
    ]
    assert len(failed) == 1
    assert failed[0].success is False
    assert failed[0].error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
    assert result["run_error"] is None
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED


def test_overlong_reason_truncated() -> None:
    """超长 reason 被工具函数截断，工具成功、评价不丢失、审计字段有界。"""
    long_reason = "理由" * 300  # 600 个字符，远超 200 字符上限，触发截断
    model = ScriptedModel(
        _evaluation_script("pass", "pass", "pass", long_reason)
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请评价这段回答", "eval-long-reason")

    assert result["evaluation"] is not None
    assert result["evaluation"].reason == "理由" * 100
    assert len(result["evaluation"].reason) == 200
    assert _evaluation_events(result)[0].evaluation_verdict == "pass"


# ── 计划步骤中的评价（Worker 链：助教 → 评价） ──────────────


def _plan_response() -> AIMessage:
    """模型调用 create_task_plan 创建「讲解 → 检查」两步骤计划。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_task_plan",
                "args": {
                    "steps": [
                        {
                            "sequence": 1,
                            "description": "讲解梯度下降",
                            "target_agent": "teaching_assistant",
                        },
                        {
                            "sequence": 2,
                            "description": "检查讲解准确性",
                            "target_agent": "evaluator",
                        },
                    ]
                },
                "id": "eval-plan",
                "type": "tool_call",
            }
        ],
    )


def test_plan_step_evaluator_produces_evaluation() -> None:
    """计划模式：evaluator 作为计划步骤执行时同样产出结构化评价。"""
    model = ScriptedModel(
        [
            _plan_response(),
            AIMessage(content="计划已创建"),
            AIMessage(content="教学结果"),
            _submit_response("pass", "pass", "pass", "讲解准确且引用完整"),
            AIMessage(content="评价完成。"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请先讲解梯度下降，再检查讲解是否准确", "eval-plan-step")

    assert result["run_error"] is None
    assert _switched_agents(result) == [
        "teaching_assistant",
        "supervisor",
        "evaluator",
        "supervisor",
    ]
    evaluation = result["evaluation"]
    assert evaluation is not None
    assert evaluation.verdict == EvaluationVerdict.PASS
    assert evaluation.reason == "讲解准确且引用完整"
    eval_events = _evaluation_events(result)
    assert len(eval_events) == 1
    assert eval_events[0].agent == AgentRole.EVALUATOR.value
    assert result["messages"][-1].content == "最终汇总"
