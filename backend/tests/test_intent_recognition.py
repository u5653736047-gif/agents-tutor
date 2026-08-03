"""S2-T1 Supervisor 教学意图识别测试。

覆盖验收标准：
1. 意图集合定义（至少四类 + 意图不明），写在 state 与 Supervisor prompt 中；
2. 意图识别结果写入 state["intent"] 与 INTENT_DETECTED 运行事件；
3. 路由以意图为主要依据：答疑 → 直接回答或 learning_assistant，
   备课 → teaching_assistant，评价 → evaluator；意图不明 → 追问而非分派
   （含「模型仍强行分派」被运行时拦截的硬保障）；
4. 意图字段随 checkpoint 持久化、每轮重置。

全部使用确定性替身模型（ScriptedModel），不依赖真实模型。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

from core.events import ErrorCode, EventType
from core.graph_builder import CollaborativeAgentGraph
from core.nodes.prompts import ROLE_PROMPTS
from core.state import AgentRole, Intent


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


def _intent_response(intent: str, reason: str = "") -> AIMessage:
    """模型调用 detect_intent 工具并自报意图。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "detect_intent",
                "args": {"intent": intent, "reason": reason},
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


def _plan_response() -> AIMessage:
    """模型调用 create_task_plan 工具创建两步骤计划。"""
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
                "id": "intent-plan",
                "type": "tool_call",
            }
        ],
    )


def _intent_events(result: dict, *, after_sequence: int = -1) -> list:
    """过滤出 INTENT_DETECTED 事件。

    after_sequence：只统计 sequence 大于该值的事件。run() 返回的 events
    是跨轮累积（events 通道按 operator.add 追加，checkpoint 全量保留），
    因此多轮会话断言「本轮新增」时必须按 sequence 差分——与 api 层
    _public_events 的消费方式一致。
    """
    return [
        event
        for event in result["events"]
        if event.event_type is EventType.INTENT_DETECTED
        and event.sequence > after_sequence
    ]


def _switched_agents(result: dict) -> list[str]:
    """按顺序列出 AGENT_SWITCHED 事件的目标。"""
    return [
        event.agent
        for event in result["events"]
        if event.event_type is EventType.AGENT_SWITCHED
    ]


# ── 意图集合与工具契约 ─────────────────────────────────────


def test_intent_enum_covers_required_categories() -> None:
    """验收：意图集合至少包含答疑、备课、评价、其他，另有意图不明。"""
    values = {intent.value for intent in Intent}
    assert {
        Intent.ANSWER_QUESTION,
        Intent.LESSON_PREP,
        Intent.EVALUATION,
        Intent.OTHER,
    } <= set(Intent)
    assert Intent.UNCLEAR in Intent  # 意图不明是独立类别，不是「其他」
    assert "answer_question" in values
    assert "lesson_prep" in values
    assert "evaluation" in values
    assert "other" in values
    assert "unclear" in values


def test_supervisor_prompt_defines_intent_contract() -> None:
    """验收：意图集合写在 prompts 中，含五类值与「不明只追问、禁止分派」。"""
    prompt = ROLE_PROMPTS[AgentRole.SUPERVISOR]
    for value in ("answer_question", "lesson_prep", "evaluation", "other", "unclear"):
        assert value in prompt
    assert "detect_intent" in prompt
    assert "禁止" in prompt  # 意图不明时禁止分派的硬性约定


def test_detect_intent_tool_is_supervisor_only() -> None:
    """detect_intent 注册且仅 Supervisor 可调用。"""
    graph = CollaborativeAgentGraph(model=ScriptedModel([]))
    tool = graph.registry.get("detect_intent")

    assert tool is not None
    assert tool.args_schema is not None
    schema_text = str(tool.args_schema.model_json_schema())
    assert "answer_question" in schema_text
    assert "lesson_prep" in schema_text
    assert graph.registry.is_authorized("detect_intent", AgentRole.SUPERVISOR)
    assert not graph.registry.is_authorized(
        "detect_intent", AgentRole.TEACHING_ASSISTANT
    )
    assert not graph.registry.is_authorized("detect_intent", AgentRole.EVALUATOR)
    assert "detect_intent" in model_bound_names(graph)


def model_bound_names(graph: CollaborativeAgentGraph) -> list[str]:
    # bind_tools 在构造时执行一次，这里直接查 Supervisor Agent 的绑定记录。
    # cast 到 ScriptedModel：ChatModel 协议不声明 bound_tool_names，
    # 但 factory 的 _bind_tools 返回的正是 ScriptedModel 本身。
    bound = cast(ScriptedModel, graph.agents[AgentRole.SUPERVISOR].model)
    return bound.bound_tool_names


# ── 各意图的路由决策 ───────────────────────────────────────


def test_answer_question_intent_answers_directly() -> None:
    """答疑 → 直接回答：不 handoff、不建计划，意图写入 state 与事件。"""
    model = ScriptedModel(
        [
            _intent_response("answer_question", "学生问梯度下降怎么理解"),
            AIMessage(content="梯度下降是一种迭代优化算法……"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("老师，梯度下降怎么理解？", "intent-answer-direct")

    assert result["intent"] == Intent.ANSWER_QUESTION
    assert result["messages"][-1].content == "梯度下降是一种迭代优化算法……"
    assert _switched_agents(result) == []
    assert result["handoff_count"] == 0
    assert result["task_plan"] is None
    assert result["task_context"] is None  # 未分派，不产生任务上下文
    assert result["run_error"] is None
    intent_events = _intent_events(result)
    assert len(intent_events) == 1
    assert intent_events[0].agent == AgentRole.SUPERVISOR.value
    assert intent_events[0].intent == Intent.ANSWER_QUESTION.value
    assert intent_events[0].success is True


def test_answer_question_intent_routes_to_learning_assistant() -> None:
    """答疑 → 助学 Agent（learning_assistant）：分派目标与意图匹配。"""
    model = ScriptedModel(
        [
            _intent_response("answer_question"),
            _handoff_response("learning_assistant"),
            AIMessage(content="任务已分派"),
            AIMessage(content="已经为您制定了学习计划"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("我数学基础差，帮我规划一下怎么学微积分", "intent-answer-handoff")

    assert result["intent"] == Intent.ANSWER_QUESTION
    assert _switched_agents(result) == ["learning_assistant", "supervisor"]
    assert result["handoff_count"] == 1
    # 锁住「聚合轮不重复发 INTENT_DETECTED」：意图事件只在该轮识别时发一次
    assert len(_intent_events(result)) == 1
    assert result["task_context"] is not None
    assert result["task_context"].intent == Intent.ANSWER_QUESTION.value
    assert result["run_error"] is None


def test_lesson_prep_intent_routes_to_teaching_assistant() -> None:
    """备课/讲解请求 → 助教 Agent（teaching_assistant）。"""
    model = ScriptedModel(
        [
            _intent_response("lesson_prep", "要一份二次函数教案"),
            _handoff_response("teaching_assistant"),
            AIMessage(content="任务已分派"),
            AIMessage(content="教案已生成"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我准备一节二次函数的教案", "intent-lesson-prep")

    assert result["intent"] == Intent.LESSON_PREP
    assert _switched_agents(result) == ["teaching_assistant", "supervisor"]
    assert result["task_context"] is not None
    assert result["task_context"].intent == Intent.LESSON_PREP.value
    assert result["run_error"] is None


def test_evaluation_intent_routes_to_evaluator() -> None:
    """评价/批改 → 评价 Agent（evaluator）。"""
    model = ScriptedModel(
        [
            _intent_response("evaluation"),
            _handoff_response("evaluator"),
            AIMessage(content="任务已分派"),
            AIMessage(content="作业评价完成"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我批改一下这篇作文", "intent-evaluation")

    assert result["intent"] == Intent.EVALUATION
    assert _switched_agents(result) == ["evaluator", "supervisor"]
    assert result["task_context"] is not None
    assert result["task_context"].intent == Intent.EVALUATION.value
    assert result["run_error"] is None


def test_other_intent_answers_directly() -> None:
    """其他意图 → 直接回答，不强行分派。"""
    model = ScriptedModel(
        [
            _intent_response("other"),
            AIMessage(content="这不是教学问题，我直接回答您。"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("今天天气怎么样", "intent-other")

    assert result["intent"] == Intent.OTHER
    assert _switched_agents(result) == []
    assert result["handoff_count"] == 0
    assert result["task_plan"] is None
    assert result["run_error"] is None


# ── 意图不明：追问而非分派 ──────────────────────────────────


def test_unclear_intent_clarifies_without_dispatch() -> None:
    """意图不明 → 生成澄清性回答，不 handoff、不建计划。"""
    model = ScriptedModel(
        [
            _intent_response("unclear"),
            AIMessage(content="请问您是想让我讲解某个知识点，还是需要我评价作业呢？"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我看看这个", "intent-unclear")

    assert result["intent"] == Intent.UNCLEAR
    assert "请问" in str(result["messages"][-1].content)
    assert _switched_agents(result) == []
    assert result["handoff_count"] == 0
    assert result["task_plan"] is None
    assert result["task_context"] is None
    assert result["run_error"] is None
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED
    intent_events = _intent_events(result)
    assert len(intent_events) == 1
    assert intent_events[0].intent == Intent.UNCLEAR.value


def test_unclear_intent_blocks_stubborn_handoff() -> None:
    """硬保障：模型自报不明仍强行 handoff → 分派被拦截，正常收口。"""
    model = ScriptedModel(
        [
            _intent_response("unclear"),
            _handoff_response("learning_assistant"),
            AIMessage(content="请再说明一下您的需求，我好准确帮您。"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("随便帮我处理一下", "intent-unclear-blocked-handoff")

    assert result["intent"] == Intent.UNCLEAR
    # 拦截点：不产生任何 Agent 切换，计数保持 0
    assert _switched_agents(result) == []
    assert result["handoff_count"] == 0
    assert result["agent_switch_count"] == 0
    assert result["messages"][-1].content == "请再说明一下您的需求，我好准确帮您。"
    assert result["run_error"] is None
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED


def test_unclear_intent_blocks_stubborn_task_plan() -> None:
    """硬保障：模型自报不明仍强行 create_task_plan → 计划被丢弃。"""
    model = ScriptedModel(
        [
            _intent_response("unclear"),
            _plan_response(),
            AIMessage(content="您的意思我还不太确定，能否再说明一下？"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我弄一下那个东西", "intent-unclear-blocked-plan")

    assert result["intent"] == Intent.UNCLEAR
    assert result["task_plan"] is None
    assert result["task_results"] == []
    assert _switched_agents(result) == []
    assert result["handoff_count"] == 0
    assert result["run_error"] is None
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED


def test_unclear_intent_repeated_dispatch_hits_iteration_limit() -> None:
    """边界：意图不明后模型持续输出工具调用 → 迭代超限失败，不死循环。

    意图不明时拦截只丢弃分派、不终止循环；若模型每轮都继续调用
    detect_intent，ReAct 循环达到 max_iterations 后走既有失败路径，
    以稳定错误码收口，证明不存在无限循环。
    """
    model = ScriptedModel(
        [
            _intent_response("unclear"),
            _intent_response("unclear"),
            _intent_response("unclear"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, max_iterations=3)

    result = graph.run("帮我看看", "intent-unclear-iteration-limit")

    assert result["intent"] == Intent.UNCLEAR
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.REACT_ITERATION_LIMIT
    assert _switched_agents(result) == []
    assert result["handoff_count"] == 0
    assert result["events"][-1].event_type is EventType.RUN_FAILED
    assert result["events"][-1].error_code is ErrorCode.REACT_ITERATION_LIMIT
    assert result["events"][-1].success is False


# ── 意图生命周期：持久化与重置 ──────────────────────────────


def test_intent_persists_in_checkpoint_and_resets_per_turn() -> None:
    """意图随 checkpoint 持久化（get_state 可读），新轮重新识别并重置。"""
    model = ScriptedModel(
        [
            _intent_response("lesson_prep"),
            AIMessage(content="教案完成"),
            # 第二轮：模型不再调用 detect_intent（模拟未识别）
            AIMessage(content="第二轮的普通回答"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())
    session_id = "intent-lifecycle"
    user_id = "user-1"

    first = graph.run("准备一份教案", session_id, user_id)

    assert first["intent"] == Intent.LESSON_PREP
    persisted = graph.get_state(session_id, user_id)
    assert persisted is not None
    assert persisted["intent"] == Intent.LESSON_PREP

    second = graph.run("你好", session_id, user_id)

    # 新轮开始 intent 被重置为 None，未识别则不写入；
    # run() 返回的 events 是跨轮累积（含第一轮遗留的 INTENT_DETECTED），
    # 因此断言「本轮新增」需按 sequence 差分：全量仍有 1 条（第一轮），
    # 而 sequence 大于第一轮末尾的事件中没有任何意图事件。
    assert second["intent"] is None
    assert second["messages"][-1].content == "第二轮的普通回答"
    first_last_sequence = max(event.sequence for event in first["events"])
    assert len(_intent_events(second)) == 1  # 第一轮遗留
    assert _intent_events(second, after_sequence=first_last_sequence) == []


def test_invalid_intent_value_is_rejected_without_crash() -> None:
    """写入端严格：非法意图值被工具层拒绝，运行宽容降级为无意图。"""
    model = ScriptedModel(
        [
            _intent_response("not_a_valid_intent"),
            AIMessage(content="收到"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我个忙", "intent-invalid")

    assert result["intent"] is None
    assert _intent_events(result) == []
    assert result["tool_results"][0].success is False
    assert result["tool_results"][0].error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
    assert result["run_error"] is None
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED


def test_overlong_reason_is_truncated_without_losing_intent() -> None:
    """超长 reason 被工具函数截断，工具成功、意图识别不丢失。"""
    long_reason = "理由" * 300  # 600 个字符，远超 200 字符上限，触发截断
    model = ScriptedModel(
        [
            _intent_response("lesson_prep", long_reason),
            AIMessage(content="教案任务已收到"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我准备教案", "intent-long-reason")

    assert result["intent"] == Intent.LESSON_PREP
    assert len(_intent_events(result)) == 1
    assert result["tool_results"][0].success is True
    payload = json.loads(result["tool_results"][0].output)
    assert payload["intent"] == Intent.LESSON_PREP.value
    # 截断到 200 字符（"理由" 每个占 2 字符，故为 100 个 "理由"），审计字段有界
    assert payload["reason"] == "理由" * 100
    # 直接校验字符数等于上限 200，防止乘法笔误导致期望值漂移
    assert len(payload["reason"]) == 200
