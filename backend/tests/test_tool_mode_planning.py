"""S5-A1/A2/A3 tool 模式任务计划（创建、门控、失败策略、上下文增强）测试。

覆盖清单：
1. A1 正常路径：create_task_plan → 按序 ask_* → 计划 COMPLETED；
   TASK_PLAN_CREATED / TASK_RESULT_ARCHIVED / TASK_RESULTS_AGGREGATED
   事件齐备；state 的 task_plan/task_results 填充完整（A4 数据源）；
2. A1 门控：乱序 ask_* 被拒（模型可读 JSON，含期望目标）；ACTIVE 计划
   期间重复 create_task_plan 被拒；纠偏后按序执行成功；
3. A2 失败策略：abort 熔断（计划 FAILED + 后续 ask_* 硬拒绝 + 不发
   RUN_FAILED）、continue 推进游标（聚合 degraded）、retry 预算内重试
   成功（retries_used 记账、结果不重复）与预算耗尽收口；
4. A3 上下文增强：子代理消息 = 最近有界对话 + 任务消息；剔除工具消息、
   单条截断、总量有界（_recent_context_messages 单元断言 + 集成断言）。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from core.events import ErrorCode, EventType
from core.graph_builder import CollaborativeAgentGraph, _recent_context_messages
from core.state import (
    AgentRole,
    TaskPlan,
    TaskPlanStatus,
    TaskStepResult,
)
from tests.test_graph_builder import ScriptedModel


def plan_step(
    sequence: int,
    target: str,
    description: str,
    on_failure: str = "abort",
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "description": description,
        "target_agent": target,
        "on_failure": on_failure,
    }


def create_plan_response(steps: list[dict[str, Any]]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_task_plan",
                "args": {"steps": steps},
                "id": "plan-call",
                "type": "tool_call",
            }
        ],
    )


def ask_response(tool_name: str, task: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": tool_name,
                "args": {"task": task},
                "id": f"ask-{tool_name}",
                "type": "tool_call",
            }
        ],
    )


def tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [
        message
        for message in state["messages"]
        if isinstance(message, ToolMessage)
    ]


# ── 1. A1 正常路径 ────────────────────────────────────────────────


def test_tool_mode_plan_created_executed_and_completed() -> None:
    """计划创建后按序执行两个步骤：状态推进、事件齐备、结果完整。"""
    model = ScriptedModel(
        [
            create_plan_response(
                [
                    plan_step(1, "learning_assistant", "讲解概念"),
                    plan_step(2, "teaching_assistant", "生成示例"),
                ]
            ),
            ask_response("ask_learning_assistant", "讲解梯度下降"),
            AIMessage(content="学习助手的概念讲解"),
            ask_response("ask_teaching_assistant", "出两道例题"),
            AIMessage(content="教学助手的例题内容"),
            AIMessage(content="整合回答：概念讲解 + 例题"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, orchestration_mode="tool")

    state = graph.run("先讲概念再出例题", session_id="plan-session")

    # 计划状态推进到 COMPLETED，游标走满。
    plan = TaskPlan.model_validate(state["task_plan"])
    assert plan.status is TaskPlanStatus.COMPLETED
    assert plan.current_step_index == 2
    # 结果逐条落账，输出为子代理回传负载。
    results = [TaskStepResult.model_validate(item) for item in state["task_results"]]
    assert [item.success for item in results] == [True, True]
    assert [item.step_sequence for item in results] == [1, 2]
    assert "学习助手" in (results[0].output or "")
    # 事件链：创建 → 逐步归档 → 聚合。
    event_types = [event.event_type for event in state["events"]]
    assert EventType.TASK_PLAN_CREATED in event_types
    archived = [
        event for event in state["events"]
        if event.event_type == EventType.TASK_RESULT_ARCHIVED
    ]
    assert [event.plan_step_sequence for event in archived] == [1, 2]
    assert all(event.success for event in archived)
    assert EventType.TASK_RESULTS_AGGREGATED in event_types
    assert EventType.RUN_COMPLETED in event_types
    # 创建事件只记步骤数（脱敏），不携带计划正文。
    created = next(
        event for event in state["events"]
        if event.event_type == EventType.TASK_PLAN_CREATED
    )
    assert created.content == "2"
    # 最终回答由 Supervisor 整合产出。
    assert state["messages"][-1].content == "整合回答：概念讲解 + 例题"


def test_tool_mode_gate_rejects_out_of_order_ask_then_recovers() -> None:
    """乱序 ask_* 被工具层拒绝（JSON 含期望目标），纠偏后按序完成。"""
    model = ScriptedModel(
        [
            create_plan_response(
                [
                    plan_step(1, "learning_assistant", "先讲解"),
                    plan_step(2, "teaching_assistant", "后出题"),
                ]
            ),
            # 故意乱序：第一步还没做就调教学助手。
            ask_response("ask_teaching_assistant", "跳步出题"),
            # 读到拒绝理由后纠偏。
            ask_response("ask_learning_assistant", "讲解梯度下降"),
            AIMessage(content="学习助手讲解"),
            ask_response("ask_teaching_assistant", "出两道例题"),
            AIMessage(content="教学助手例题"),
            AIMessage(content="整合完成"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, orchestration_mode="tool")

    state = graph.run("先讲解再出题", session_id="gate-session")

    rejection = next(
        message.content
        for message in tool_messages(state)
        if "expected_target" in str(message.content)
    )
    payload = json.loads(str(rejection))
    assert payload["expected_target"] == "learning_assistant"
    assert payload["current_step_sequence"] == 1
    # 纠偏后计划正常走完，结果无缺失。
    plan = TaskPlan.model_validate(state["task_plan"])
    assert plan.status is TaskPlanStatus.COMPLETED
    results = [TaskStepResult.model_validate(item) for item in state["task_results"]]
    assert [item.success for item in results] == [True, True]


def test_tool_mode_rejects_second_plan_while_active() -> None:
    """ACTIVE 计划期间重复创建被拒（冲突语义），原计划继续可用。"""
    model = ScriptedModel(
        [
            create_plan_response(
                [
                    plan_step(1, "learning_assistant", "讲解"),
                    plan_step(2, "teaching_assistant", "出题"),
                ]
            ),
            # 同轮重复创建 → 工具层拒绝。
            create_plan_response(
                [
                    plan_step(1, "teaching_assistant", "另起炉灶"),
                    plan_step(2, "teaching_assistant", "再起炉灶"),
                ]
            ),
            ask_response("ask_learning_assistant", "讲解"),
            AIMessage(content="学习助手讲解"),
            ask_response("ask_teaching_assistant", "出题"),
            AIMessage(content="教学助手出题"),
            AIMessage(content="整合完成"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, orchestration_mode="tool")

    state = graph.run("讲完出题", session_id="conflict-session")

    rejection = next(
        message.content
        for message in tool_messages(state)
        if "不允许覆盖" in str(message.content)
    )
    assert json.loads(str(rejection))["active_plan_steps"] == 2
    # 原计划不受影响，正常完成。
    plan = TaskPlan.model_validate(state["task_plan"])
    assert plan.status is TaskPlanStatus.COMPLETED
    assert plan.steps[0].target_agent is AgentRole.LEARNING_ASSISTANT


# ── 2. A2 失败策略 ────────────────────────────────────────────────


def _run_with_first_step_failure(on_failure: str) -> tuple[dict[str, Any], list[Any]]:
    """公共脚本：第一步的子代理产出空回答（执行失败），观察策略处置。

    返回 (最终 state, 教学助手收到的消息列表)——后者用于断言熔断/继续。
    """
    model = ScriptedModel(
        [
            create_plan_response(
                [
                    plan_step(1, "learning_assistant", "讲解", on_failure=on_failure),
                    plan_step(2, "teaching_assistant", "出题"),
                ]
            ),
            ask_response("ask_learning_assistant", "讲解"),
            AIMessage(content=""),  # 子代理空输出 → RuntimeError → 步骤失败
            ask_response("ask_teaching_assistant", "出题"),
            AIMessage(content="教学助手出题"),
            AIMessage(content="整合回答"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, orchestration_mode="tool")
    state = graph.run("讲解并出题", session_id=f"failure-{on_failure}")
    return state, model.calls


def test_failure_abort_marks_plan_failed_and_hard_stops() -> None:
    """abort：计划 FAILED、后续 ask_* 硬拒绝、不发 RUN_FAILED（运行不判死）。"""
    state, _ = _run_with_first_step_failure("abort")

    plan = TaskPlan.model_validate(state["task_plan"])
    assert plan.status is TaskPlanStatus.FAILED
    results = [TaskStepResult.model_validate(item) for item in state["task_results"]]
    assert len(results) == 1 and not results[0].success
    # 空输出 → AGENT_OUTPUT_INVALID（TaskStepResult 校验器允许的可归档分类）
    assert results[0].error_code is ErrorCode.AGENT_OUTPUT_INVALID
    # 熔断 ≠ 运行失败：RUN_COMPLETED 收口，无 RUN_FAILED。
    event_types = [event.event_type for event in state["events"]]
    assert EventType.RUN_COMPLETED in event_types
    assert EventType.RUN_FAILED not in event_types
    assert EventType.TASK_RESULTS_AGGREGATED not in event_types
    # 后续 ask_teaching_assistant 被硬熔断（模型收到「已结束」拒绝）。
    assert any(
        "任务计划已结束" in str(message.content)
        for message in tool_messages(state)
    )
    # Supervisor 用已有信息作答，运行正常结束（脚本第 5 项成为终态回答）。
    assert state["messages"][-1].content == "教学助手出题"


def test_failure_continue_advances_cursor_and_completes() -> None:
    """continue：记失败结果后推进游标，后续步骤照常，聚合带 degraded。"""
    state, _ = _run_with_first_step_failure("continue")

    plan = TaskPlan.model_validate(state["task_plan"])
    assert plan.status is TaskPlanStatus.COMPLETED
    results = [TaskStepResult.model_validate(item) for item in state["task_results"]]
    assert [(item.step_sequence, item.success) for item in results] == [(1, False), (2, True)]
    aggregated = next(
        event for event in state["events"]
        if event.event_type == EventType.TASK_RESULTS_AGGREGATED
    )
    assert aggregated.degraded is True


def test_failure_retry_succeeds_within_budget() -> None:
    """retry：首次失败不落结果、计一次预算；重试成功后计划正常完成。"""
    model = ScriptedModel(
        [
            create_plan_response(
                [
                    plan_step(1, "learning_assistant", "讲解", on_failure="retry"),
                    plan_step(2, "teaching_assistant", "出题"),
                ]
            ),
            ask_response("ask_learning_assistant", "讲解"),
            AIMessage(content=""),  # 第一次失败 → 消耗重试预算
            ask_response("ask_learning_assistant", "再试一次讲解"),
            AIMessage(content="重试后的讲解"),
            ask_response("ask_teaching_assistant", "出题"),
            AIMessage(content="教学助手出题"),
            AIMessage(content="整合回答"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, orchestration_mode="tool")
    state = graph.run("讲解并出题", session_id="retry-success")

    plan = TaskPlan.model_validate(state["task_plan"])
    assert plan.status is TaskPlanStatus.COMPLETED
    assert plan.retries_used == 1
    # 中间失败不落结果：每步恰好一条成功记录。
    results = [TaskStepResult.model_validate(item) for item in state["task_results"]]
    assert [(item.step_sequence, item.success) for item in results] == [(1, True), (2, True)]


def test_failure_retry_exhausted_budget_falls_back_to_abort() -> None:
    """retry 预算耗尽：第二次失败按 abort 收口（计划 FAILED）。"""
    model = ScriptedModel(
        [
            create_plan_response(
                [
                    plan_step(1, "learning_assistant", "讲解", on_failure="retry"),
                    plan_step(2, "teaching_assistant", "出题"),
                ]
            ),
            ask_response("ask_learning_assistant", "讲解"),
            AIMessage(content=""),  # 第一次失败 → 消耗预算
            ask_response("ask_learning_assistant", "再试一次"),
            AIMessage(content=""),  # 第二次失败 → 预算耗尽 → abort
            ask_response("ask_teaching_assistant", "出题"),
            AIMessage(content="教学助手出题"),
            AIMessage(content="部分整合回答"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, orchestration_mode="tool")
    state = graph.run("讲解并出题", session_id="retry-exhausted")

    plan = TaskPlan.model_validate(state["task_plan"])
    assert plan.status is TaskPlanStatus.FAILED
    assert plan.retries_used == 1
    results = [TaskStepResult.model_validate(item) for item in state["task_results"]]
    assert len(results) == 1 and not results[0].success
    # 熔断后 ask_* 被拒，运行仍正常收口。
    assert any(
        "任务计划已结束" in str(message.content)
        for message in tool_messages(state)
    )
    assert state["messages"][-1].content == "教学助手出题"


# ── 3. A3 子代理上下文增强 ────────────────────────────────────────


def test_recent_context_messages_bounded_and_filtered() -> None:
    """单元：剔除工具/空消息、单条截断、总量有界、时间正序。"""
    from langchain_core.messages import ToolMessage

    long_text = "长" * 1500
    messages: list[Any] = [
        HumanMessage(content="第一问：什么是梯度下降？"),
        AIMessage(content="梯度下降是……"),  # 应被后续 4 条挤出窗口
        HumanMessage(content=long_text),  # 截断到 1000 字符
        AIMessage(content=""),
        ToolMessage(content="工具结果应被剔除", tool_call_id="t1"),
        AIMessage(content="第四条"),
        HumanMessage(content="最新追问"),
    ]

    picked = _recent_context_messages(messages)

    assert [type(message).__name__ for message in picked] == [
        "AIMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert picked[1].content == "长" * 1000  # 单条截断
    assert all(len(message.content) <= 1000 for message in picked)  # type: ignore[attr-defined]
    assert sum(len(str(message.content)) for message in picked) <= 4000  # 总量有界
    assert picked[-1].content == "最新追问"  # 时间正序，最新在末尾


def test_tool_orchestration_prompt_documents_plan_conventions() -> None:
    """A5：tool 模式 Supervisor 提示词包含计划使用约定（锚点词断言）。"""
    from core.nodes.prompts import TOOL_ORCHESTRATION_SUPERVISOR_PROMPT

    assert "create_task_plan" in TOOL_ORCHESTRATION_SUPERVISOR_PROMPT
    assert "按计划顺序" in TOOL_ORCHESTRATION_SUPERVISOR_PROMPT
    assert "on_failure=continue" in TOOL_ORCHESTRATION_SUPERVISOR_PROMPT


def test_tool_mode_completed_plan_replayable_from_checkpoint() -> None:
    """A4 回放链路：完成后经 checkpoint 可读回完整计划与结果（SessionProcess 数据源）。"""
    model = ScriptedModel(
        [
            create_plan_response(
                [
                    plan_step(1, "learning_assistant", "讲解"),
                    plan_step(2, "teaching_assistant", "出题"),
                ]
            ),
            ask_response("ask_learning_assistant", "讲解"),
            AIMessage(content="学习助手讲解"),
            ask_response("ask_teaching_assistant", "出题"),
            AIMessage(content="教学助手出题"),
            AIMessage(content="整合完成"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        orchestration_mode="tool",
        checkpointer=InMemorySaver(),
    )
    graph.run("先讲后练", session_id="replay-session")

    # sessions.py 的 SessionProcess 经同一入口读回：计划与结果可回放。
    replayed = graph.get_state("replay-session", user_id=None)
    assert replayed is not None
    plan = TaskPlan.model_validate(replayed.get("task_plan"))
    assert plan.status is TaskPlanStatus.COMPLETED
    results = [
        TaskStepResult.model_validate(item)
        for item in replayed.get("task_results", [])
    ]
    assert [item.success for item in results] == [True, True]


def test_subagent_sees_recent_conversation_before_task() -> None:
    """集成：多轮追问时子代理消息 = 近期对话 + 任务消息（任务在最后）。"""
    model = ScriptedModel(
        [
            # 第一轮：直接回答（建立对话历史）。
            AIMessage(content="梯度下降是一阶优化算法。"),
            # 第二轮：追问转子代理。
            ask_response("ask_learning_assistant", "再讲细一点"),
            AIMessage(content="更详细的讲解"),
            AIMessage(content="这是更详细的讲解"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        orchestration_mode="tool",
        checkpointer=InMemorySaver(),
    )

    graph.run("什么是梯度下降？", session_id="context-session")
    graph.run("再讲细一点", session_id="context-session")

    # 找到子代理轮的模型调用（含「更细」任务消息的那次）：其首条消息
    # 应携带上一轮对话片段，最后一条才是任务本身。
    subagent_call = next(
        call
        for call in model.calls
        if call and str(call[-1].content).startswith("再讲细一点")
    )
    assert len(subagent_call) >= 2
    prior_contents = [str(message.content) for message in subagent_call[:-1]]
    assert any("梯度下降是一阶优化算法" in content for content in prior_contents)
