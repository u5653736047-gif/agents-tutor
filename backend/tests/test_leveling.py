"""S2-T2 助学 Agent 分层讲解测试。

覆盖验收标准：
1. 学生水平信号（StudentLevel 枚举：基础/进阶/未知）写入 state["level"]，
   随 checkpoint 持久化、跨轮保留（新轮不重置，与 intent 每轮重置相反）；
2. learning_assistant 提示词按水平分层（基础重直觉类比、进阶重推导与
   边界条件、无水平默认中等深度并说明可调整），动态部分由
   learning_assistant_system_prompt 按 state["level"] 生成；
3. 同一知识点问题在不同水平设定下，发给 learning_assistant 的系统提示词
   包含对应水平的专属指令（确定性替身模型断言消息内容，不依赖真实模型
   的输出玄学）；
4. 水平与意图/路由协同：答疑路由到 learning_assistant 时水平生效，
   备课路由到 teaching_assistant 时不注入水平指令。

全部使用确定性替身模型（ScriptedModel），不依赖真实模型。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.checkpoint.memory import InMemorySaver

from core.events import ErrorCode, EventType
from core.graph_builder import CollaborativeAgentGraph
from core.nodes.prompts import (
    _LEVEL_GUIDANCE,
    ROLE_PROMPTS,
    learning_assistant_system_prompt,
)
from core.state import AgentRole, AgentState, Intent, StudentLevel


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


def _level_response(level: str, reason: str = "") -> AIMessage:
    """模型调用 detect_level 工具并自报学生水平。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "detect_level",
                "args": {"level": level, "reason": reason},
                "id": f"level-{level}",
                "type": "tool_call",
            }
        ],
    )


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


def _tutoring_handoff_script(level: str | None) -> list[AIMessage]:
    """构造「答疑 → learning_assistant 分派」的完整响应脚本。

    响应数量与 ReAct invoke 次数严格一一对应（缺一条就会在最后一次
    invoke 时 responses 为空，被当作 MODEL_CALL_FAILED）：
    - supervisor 轮：detect_level（仅当有水平）→ detect_intent →
      handoff → 无工具收尾回答（handoff 工具执行后模型还需再输出
      一条消息才能结束 supervisor 轮）；
    - learning_assistant 轮：讲解回答（无工具）；
    - supervisor 聚合轮：最终汇总（无工具）。
    """
    responses: list[AIMessage] = []
    if level is not None:
        responses.append(_level_response(level))
    responses.extend(
        [
            _intent_response("answer_question"),
            _handoff_response("learning_assistant"),
            AIMessage(content="任务已分派"),
            AIMessage(content="已经为您制定了学习计划"),
            AIMessage(content="最终汇总"),
        ]
    )
    return responses


def _switched_agents(result: dict) -> list[str]:
    """按顺序列出 AGENT_SWITCHED 事件的目标。"""
    return [
        event.agent
        for event in result["events"]
        if event.event_type is EventType.AGENT_SWITCHED
    ]


def _learning_assistant_system_prompts(model: ScriptedModel) -> list[str]:
    """取出所有发给 learning_assistant 的 system prompt 文本。

    识别方式：只有助学 Agent 的动态提示词含「[当前学生水平:」标记
    （见 prompts.learning_assistant_system_prompt），其余角色的 system
    prompt 不含该标记，因此直接按它过滤，不依赖调用顺序索引。
    替身模型在测试中直接回答（不调工具），learning_assistant 只被调用
    一次，故列表通常恰含一条。
    """
    return [
        str(messages[0].content)
        for messages in model.calls
        if messages
        and isinstance(messages[0], SystemMessage)
        and "[当前学生水平:" in str(messages[0].content)
    ]


# ── 水平集合与 Prompt 契约 ─────────────────────────────────


def test_student_level_enum_covers_required_categories() -> None:
    """验收：水平集合至少含基础/进阶两档，另有默认未知。"""
    assert {level.value for level in StudentLevel} == {
        "basic",
        "advanced",
        "unknown",
    }


def test_learning_assistant_prompt_defines_leveling_contract() -> None:
    """验收：分层策略写在助学 Agent 提示词中（三类水平的讲解约定）。"""
    prompt = ROLE_PROMPTS[AgentRole.LEARNING_ASSISTANT]
    assert "直觉类比" in prompt  # 基础水平：重直觉类比
    assert "推导" in prompt  # 进阶水平：重推导
    assert "边界条件" in prompt  # 进阶水平：重边界条件
    assert "中等深度" in prompt  # 无水平信息：默认中等深度
    assert "调整" in prompt  # 并说明可调整


def test_level_guidance_covers_all_student_levels() -> None:
    """守卫：_LEVEL_GUIDANCE 档位指导词与 StudentLevel 枚举一一对应。

    未来新增水平档位时，若只加枚举值而忘记在 prompts.py 补充对应讲解
    策略，_LEVEL_GUIDANCE 缺键会让 learning_assistant_system_prompt
    静默退化为 unknown（默认中等深度）——本测试锁住该同步。
    """
    assert set(_LEVEL_GUIDANCE) == {level.value for level in StudentLevel}


def test_supervisor_prompt_mentions_level_recording() -> None:
    """Supervisor 提示词告知模型何时调用 detect_level 记录水平画像。"""
    assert "detect_level" in ROLE_PROMPTS[AgentRole.SUPERVISOR]


def test_detect_level_tool_is_supervisor_only() -> None:
    """detect_level 注册且仅 Supervisor 可调用。"""
    graph = CollaborativeAgentGraph(model=ScriptedModel([]))
    tool = graph.registry.get("detect_level")

    assert tool is not None
    assert tool.args_schema is not None
    schema_text = str(tool.args_schema.model_json_schema())
    assert "basic" in schema_text
    assert "advanced" in schema_text
    assert "unknown" in schema_text
    assert graph.registry.is_authorized("detect_level", AgentRole.SUPERVISOR)
    assert not graph.registry.is_authorized(
        "detect_level", AgentRole.TEACHING_ASSISTANT
    )
    assert not graph.registry.is_authorized("detect_level", AgentRole.EVALUATOR)
    bound = cast(ScriptedModel, graph.agents[AgentRole.SUPERVISOR].model)
    assert "detect_level" in bound.bound_tool_names


# ── 动态提示词函数单元测试 ──────────────────────────────────


def test_leveled_prompt_normalizes_missing_level_to_unknown() -> None:
    """无水平信息（None 或 unknown）→ 默认中等深度指令，并说明可调整。"""
    for level in (None, "unknown"):
        prompt = learning_assistant_system_prompt(level)
        assert "[当前学生水平:unknown]" in prompt
        assert "中等深度" in prompt
        assert "按学生反馈调整" in prompt


def test_leveled_prompt_carries_basic_and_advanced_guidance() -> None:
    """基础/进阶分别携带各自档位的专属讲解指令。"""
    basic = learning_assistant_system_prompt("basic")
    assert "[当前学生水平:basic]" in basic
    assert "生活化类比" in basic  # basic 档独有锚点词

    advanced = learning_assistant_system_prompt("advanced")
    assert "[当前学生水平:advanced]" in advanced
    assert "严谨推导" in advanced  # advanced 档独有锚点词


def test_leveled_prompt_falls_back_for_unknown_values() -> None:
    """历史脏数据（非枚举字符串）宽容归一为 unknown，不抛错。"""
    prompt = learning_assistant_system_prompt("expert")
    assert "[当前学生水平:unknown]" in prompt


# ── 水平写入 state 与 task_context 联动 ─────────────────────


def test_level_written_to_state_and_shared_with_task_context() -> None:
    """答疑分派时：level 写入 state，并与意图一起同步进 task_context。"""
    model = ScriptedModel(_tutoring_handoff_script("basic"))
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("我基础差，帮我讲讲梯度下降", "level-task-context")

    assert result["level"] == StudentLevel.BASIC.value
    assert result["task_context"] is not None
    assert result["task_context"].level == StudentLevel.BASIC.value
    assert result["task_context"].intent == Intent.ANSWER_QUESTION.value
    assert result["run_error"] is None


def test_level_recorded_even_without_dispatch() -> None:
    """无分派（直接回答）轮也记录水平：跨轮画像不依赖任务分派。"""
    model = ScriptedModel(
        [
            _level_response("basic"),
            AIMessage(content="直接回答：导数就是变化率……"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("我基础差，什么是导数？", "level-no-dispatch")

    assert result["level"] == StudentLevel.BASIC.value
    assert result["task_context"] is None  # 未分派，不产生任务上下文
    assert result["handoff_count"] == 0


# ── 水平生命周期：持久化与跨轮保留 ──────────────────────────


def test_level_persists_in_checkpoint_and_survives_new_turns() -> None:
    """水平随 checkpoint 持久化，且新轮不重置（与 intent 语义相反）。"""
    model = ScriptedModel(
        [
            _level_response("advanced"),
            AIMessage(content="第一轮直接回答"),
            # 第二轮：模型不再调用 detect_level（模拟未自报新水平），
            # 序列与 _tutoring_handoff_script(None) 一致（intent →
            # handoff → supervisor 收尾 → worker 回答 → 聚合汇总）。
            _intent_response("answer_question"),
            _handoff_response("learning_assistant"),
            AIMessage(content="任务已分派"),
            AIMessage(content="学习计划"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())
    session_id = "level-lifecycle"
    user_id = "user-1"

    first = graph.run("我学得比较深，帮我讲讲收敛性", session_id, user_id)

    assert first["level"] == StudentLevel.ADVANCED.value
    persisted = graph.get_state(session_id, user_id)
    assert persisted is not None
    assert persisted["level"] == StudentLevel.ADVANCED.value

    second = graph.run("那帮我讲讲梯度下降的收敛性", session_id, user_id)

    # 关键断言：新轮不重置水平（intent 此时被重置后重新识别，
    # level 保留第一轮画像），learning_assistant 仍收到进阶指令。
    assert second["level"] == StudentLevel.ADVANCED.value
    prompts = _learning_assistant_system_prompts(model)
    assert len(prompts) == 1
    assert "[当前学生水平:advanced]" in prompts[0]
    assert "严谨推导" in prompts[0]


def test_new_level_overrides_previous_level() -> None:
    """学生自报新水平时覆盖旧画像（保留语义下的唯一更新途径）。"""
    model = ScriptedModel(
        [
            _level_response("basic"),
            AIMessage(content="第一轮回答"),
            _level_response("advanced"),
            AIMessage(content="第二轮回答"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())
    session_id = "level-override"
    user_id = "user-1"

    graph.run("我基础差", session_id, user_id)
    second = graph.run("其实我学得比较深", session_id, user_id)

    assert second["level"] == StudentLevel.ADVANCED.value
    persisted = graph.get_state(session_id, user_id)
    assert persisted is not None
    assert persisted["level"] == StudentLevel.ADVANCED.value


def test_legacy_checkpoint_without_level_channel_degrades_gracefully() -> None:
    """老状态（无 level 通道）退化：不抛错、按 unknown 讲解。

    模拟 S2-T2 之前持久化的旧数据：手工构造不含 level 键的完整状态
    字典（等效于旧版本 checkpoint 反序列化后的 channel values）直接
    入图——图正常执行，level 读取侧退化 None，助学 Agent 收到默认
    中等深度指令，task_context 快照正常写入。

    为什么不走 run()/update_state：run() 用 create_initial_state 填充
    level=None（键存在），update_state 是合并语义无法删除已有键；
    直接 app.invoke 一个缺 level 键的旧状态是最贴近「老 checkpoint 被
    新版本图加载」的等效方式（LangGraph 对未写入通道本就宽容）。
    """
    model = ScriptedModel(_tutoring_handoff_script(None))
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())
    session_id = "level-legacy"
    user_id = "user-1"
    config = graph._thread_config(session_id, user_id)
    legacy = cast(
        AgentState,
        {
            "messages": [
                HumanMessage(content="旧轮提问"),
                AIMessage(content="旧轮回答"),
                HumanMessage(content="帮我讲讲梯度下降"),
            ],
            "current_agent": None,
            "next_agent": None,
            "pending_handoff": None,
            "intent": None,
            "task_context": None,
            "task_plan": None,
            "task_results": [],
            "tool_results": [],
            "session_id": session_id,
            "user_id": user_id,
            "events": [],
            "run_error": None,
            "handoff_count": 0,
            "agent_switch_count": 0,
            "extra": {},
            # 注意：刻意不含 level 键——等效于旧版本持久化的数据
        },
    )

    result = graph.build().invoke(legacy, config=config)

    # 关键断言：旧状态无 level 键 → 不抛错、按 unknown 讲解
    assert "level" not in result
    assert result["run_error"] is None
    prompts = _learning_assistant_system_prompts(model)
    assert len(prompts) == 1
    assert "[当前学生水平:unknown]" in prompts[0]
    assert "按学生反馈调整" in prompts[0]
    assert result["task_context"] is not None
    assert result["task_context"].intent == Intent.ANSWER_QUESTION.value
    assert result["task_context"].level == ""  # 无水平时不写快照


# ── 无水平信息时的默认行为 ──────────────────────────────────


def test_no_level_defaults_to_unknown_guidance() -> None:
    """首次提问无水平信息：state 保持 None，助学 Agent 收到默认指令。"""
    model = ScriptedModel(_tutoring_handoff_script(None))
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我规划一下微积分学习", "level-default")

    assert result["level"] is None
    assert result["task_context"] is not None
    assert result["task_context"].level == ""  # 无水平时不写快照
    prompts = _learning_assistant_system_prompts(model)
    assert len(prompts) == 1
    assert "[当前学生水平:unknown]" in prompts[0]
    assert "按学生反馈调整" in prompts[0]


# ── 同一问题在不同水平下产出深度可区分（验收核心） ──────────


def _run_leveled_handoff(level: str | None, session: str) -> str:
    """跑一轮「答疑 → learning_assistant」并返回其收到的 system prompt。"""
    model = ScriptedModel(_tutoring_handoff_script(level))
    graph = CollaborativeAgentGraph(model=model)
    graph.run("帮我讲讲梯度下降", session)
    prompts = _learning_assistant_system_prompts(model)
    assert len(prompts) == 1
    return prompts[0]


def test_same_question_yields_distinct_guidance_per_level() -> None:
    """同一知识点问题：基础/进阶设定下发给模型的消息结构可区分。"""
    basic_prompt = _run_leveled_handoff("basic", "level-basic")
    advanced_prompt = _run_leveled_handoff("advanced", "level-advanced")

    # 水平标记不同：机器可读锚点直接区分两档
    assert "[当前学生水平:basic]" in basic_prompt
    assert "[当前学生水平:advanced]" in advanced_prompt
    # 基础档：带类比指令，且不含进阶档的推导指令
    assert "生活化类比" in basic_prompt
    assert "严谨推导" not in basic_prompt
    # 进阶档：带推导/边界条件指令，且不含基础档的类比指令
    assert "严谨推导" in advanced_prompt
    assert "生活化类比" not in advanced_prompt


# ── 水平与意图/路由协同 ─────────────────────────────────────


def test_level_cooperates_with_intent_routing() -> None:
    """答疑路由到 learning_assistant 时水平生效，且只作用于助学 Agent。"""
    model = ScriptedModel(_tutoring_handoff_script("basic"))
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("我基础差，帮我规划微积分学习", "level-routing")

    assert result["intent"] == Intent.ANSWER_QUESTION
    assert _switched_agents(result) == ["learning_assistant", "supervisor"]
    assert result["task_context"] is not None
    assert result["task_context"].intent == Intent.ANSWER_QUESTION.value
    assert result["task_context"].level == StudentLevel.BASIC.value
    prompts = _learning_assistant_system_prompts(model)
    assert "[当前学生水平:basic]" in prompts[0]
    assert "生活化类比" in prompts[0]


def test_level_guidance_only_applies_to_learning_assistant() -> None:
    """备课路由到 teaching_assistant：其 system prompt 不注入水平指令。"""
    model = ScriptedModel(
        [
            _intent_response("lesson_prep"),
            _handoff_response("teaching_assistant"),
            AIMessage(content="任务已分派"),
            AIMessage(content="教案已生成"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我准备一节二次函数的教案", "level-routing-ta")

    assert result["intent"] == Intent.LESSON_PREP
    assert _switched_agents(result) == ["teaching_assistant", "supervisor"]
    # 所有发给模型的 system prompt（含 supervisor 与 teaching_assistant）
    # 都不带「[当前学生水平:」标记：分层只作用于助学 Agent
    for messages in model.calls:
        assert messages and isinstance(messages[0], SystemMessage)
        assert "[当前学生水平:" not in str(messages[0].content)


# ── 写入端严格性与审计有界性 ────────────────────────────────


def test_invalid_level_value_is_rejected_without_crash() -> None:
    """写入端严格：非法水平值被工具层拒绝，运行宽容降级为水平未知。"""
    model = ScriptedModel(
        [
            _level_response("not_a_valid_level"),
            AIMessage(content="收到"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("帮我个忙", "level-invalid")

    assert result["level"] is None
    assert result["tool_results"][0].success is False
    assert result["tool_results"][0].error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
    assert result["run_error"] is None
    assert result["events"][-1].event_type is EventType.RUN_COMPLETED


def test_overlong_level_reason_is_truncated_without_losing_level() -> None:
    """超长 reason 被工具函数截断，工具成功、水平识别不丢失。"""
    long_reason = "理由" * 300  # 600 个字符，远超 200 字符上限，触发截断
    model = ScriptedModel(
        [
            _level_response("basic", long_reason),
            AIMessage(content="收到"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("我基础差", "level-long-reason")

    assert result["level"] == StudentLevel.BASIC.value
    assert result["tool_results"][0].success is True
    payload = json.loads(result["tool_results"][0].output)
    assert payload["level"] == StudentLevel.BASIC.value
    assert payload["reason"] == "理由" * 100
    assert len(payload["reason"]) == 200


# ── 动态提示词与上下文 token 预算 ───────────────────────────


def _one_token_per_message(messages: Sequence[BaseMessage]) -> int:
    """测试计数器：每条消息（含 system prompt）计 1 token。"""
    return len(messages)


def test_leveled_prompt_counts_toward_context_token_budget() -> None:
    """动态水平提示词计入 token 预算：预算紧张时历史被裁、水平指令保留。

    与 test_graph_persistence 的 token 裁剪测试同一模式，但计数器把
    system prompt 也计 1 token（trim_message_history 中 prefix_messages
    参与预算计算）：预算=2 时「动态 system(1) + 最近用户消息(1)」恰好
    占满，supervisor 轮的工具调用历史全部被裁剪——证明带水平段的动态
    提示词参与 max_context_tokens 预算，且裁剪不会丢失水平指令。
    """
    model = ScriptedModel(_tutoring_handoff_script("basic"))
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        max_context_tokens=2,
        context_token_counter=_one_token_per_message,
    )

    graph.run("帮我讲讲梯度下降", "level-token-budget")

    la_calls = [
        messages
        for messages in model.calls
        if messages
        and isinstance(messages[0], SystemMessage)
        and "[当前学生水平:" in str(messages[0].content)
    ]
    assert len(la_calls) == 1
    la_messages = la_calls[0]
    # 动态 system(1 token) + 最近用户消息(1 token) 恰好占满预算，
    # 历史中的工具调用消息全部被裁剪；水平指令完整可见
    assert len(la_messages) == 2
    assert "[当前学生水平:basic]" in str(la_messages[0].content)
    assert "生活化类比" in str(la_messages[0].content)
    assert isinstance(la_messages[1], HumanMessage)
