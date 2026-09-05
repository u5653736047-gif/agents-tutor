"""作业批改链路测试（六大功能计划 P2 验收）。

覆盖：
1. 客观题确定性比对（归一化规则，零 LLM 可复现）；
2. submit_grading schema 非法拒绝（写入端严格）；
3. handoff 模式：evaluator 轮 submit_grading → 通道写入 + 事件 +
   消息元数据挂载；
4. tool 模式（生产）：ask_evaluator 负载回传 → Supervisor 轮提取
   写通道（pi 审查 🔴 生产可见性）；
5. 确定性落库闭环（pi 审查 🔴3）：多题 fixture 全部入库（复合幂等
   键，🟡3）、落库用户来自 state 而非模型参数、store 未注入静默跳过；
6. 每轮重置 + 历史消息元数据保留（pi 审查 🟡4 刷新恢复的数据基础）。

全部使用确定性替身模型（ScriptedModel），不依赖真实模型。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from core.events import ErrorCode, EventType
from core.graph_builder import (
    CollaborativeAgentGraph,
    _answers_match,
    _GradingInput,
    _ObjectiveItemInput,
    grade_objective_answers,
)
from core.learning import LearningRecordStore
from core.state import AgentRole, message_grading
from core.tools import ToolExecutor


class ScriptedModel:
    """按图执行顺序返回预设消息。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


@pytest.fixture()
def store(tmp_path: Path) -> LearningRecordStore:
    record_store = LearningRecordStore(tmp_path / "learning.db")
    yield record_store
    record_store.close()


# ── 替身响应构造 ───────────────────────────────────────────


def _grading_response(tool_call_id: str = "grading-1") -> AIMessage:
    """evaluator 提交多题批改（含知识点维度——P3 诊断数据源）。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_grading",
                "args": {
                    "items": [
                        {
                            "question_id": "q1",
                            "score": 10,
                            "max_score": 10,
                            "feedback": "解答完整。",
                            "knowledge_point": "梯度下降",
                        },
                        {
                            "question_id": "q2",
                            "score": 0,
                            "max_score": 10,
                            "feedback": "概念错误，建议复习。",
                            "knowledge_point": "梯度下降",
                            "error_tag": "概念不清",
                        },
                        {
                            "question_id": "q3",
                            "score": 5,
                            "max_score": 10,
                            "feedback": "部分正确。",
                        },
                    ],
                    "overall_comment": "整体掌握一般。",
                },
                "id": tool_call_id,
                "type": "tool_call",
            }
        ],
    )


def _handoff_responses() -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "handoff",
                    "args": {"target": "evaluator"},
                    "id": "handoff-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="转交评价助手批改。"),
        _grading_response(),
        AIMessage(content="批改完成，请查看评分。"),
        AIMessage(content="已为你批改本次作业。"),
    ]


def _tool_mode_responses() -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ask_evaluator",
                    "args": {"task": "批改这份作业"},
                    "id": "ask-1",
                    "type": "tool_call",
                }
            ],
        ),
        # 子代理（evaluator）消费：提交批改 + 终端输出
        _grading_response(),
        AIMessage(content="已完成批改。"),
        # Supervisor 聚合
        AIMessage(content="批改结果：总分 15/30。"),
    ]


def _grading_events(result: dict) -> list:
    return [
        event
        for event in result["events"]
        if event.event_type is EventType.GRADING_COMPLETED
    ]


def _last_message_with_grading(result: dict) -> BaseMessage | None:
    """与 chat.py 的 references 两级口径一致：批改元数据挂在「产出批改
    的消息」上——handoff 模式是 evaluator 作答消息（supervisor 聚合
    回答是另一条新消息），tool 模式是 supervisor 最终回答；倒序找。"""
    for message in reversed(result["messages"]):
        if message_grading(message) is not None:
            return message
    return None


# ── 客观题确定性比对 ──────────────────────────────────────


@pytest.mark.parametrize(
    "standard,student,expected",
    [
        ("42", " 42 ", True),  # 空白归一
        ("True", "true", True),  # 大小写归一
        # 审查 W1：不做多选「集合相等」容忍——由 a-h 字母组成的英文
        # 单词与多选答案形态本质不可区分（face/bad 既是拼写题答案也是
        # 合法多选），集合比较误判会经落库污染学情数据；乱序多选由
        # 模型在调用前规范化（见 _answers_match docstring 的取舍说明）。
        ("AB", "ba", False),
        ("ABC", "cba", False),
        ("A", "a.", False),  # 单选带标点：归一后 "a." != "a"
        ("A", "b", False),
        ("42", "", False),  # 空答案
        ("机器学习", "深度学习", False),
        # 异序词不得误判为正确（旧集合比较会判 True）
        ("face", "cafe", False),
        ("bad", "dab", False),
        ("cabbage", "abbcage", False),
    ],
    ids=[
        "whitespace",
        "case",
        "multi-choice-order-strict",
        "multi-choice-three-strict",
        "single-choice-punct",
        "wrong-option",
        "empty",
        "wrong-text",
        "anagram-face-cafe",
        "anagram-bad-dab",
        "repeated-letters-word",
    ],
)
def test_answers_match_normalization_rules(
    standard: str, student: str, expected: bool
) -> None:
    assert _answers_match(standard, student) is expected


def test_grade_objective_answers_is_deterministic_and_zero_llm() -> None:
    execution = ToolExecutor([grade_objective_answers]).execute(
        {
            "name": "grade_objective_answers",
            "args": {
                "items": [
                    {
                        "question_id": "q1",
                        "standard_answer": "A",
                        "student_answer": "a",
                        "max_score": 5,
                    },
                    {
                        "question_id": "q2",
                        "standard_answer": "42",
                        "student_answer": "43",
                        "max_score": 5,
                        "answer_source": "generated",
                    },
                ]
            },
            "id": "objective-1",
        },
        AgentRole.EVALUATOR,
    )

    assert execution.result.success is True
    observation = json.loads(execution.result.output)
    assert observation["correct_count"] == 1
    assert observation["total_count"] == 2
    by_question = {item["question_id"]: item for item in observation["items"]}
    assert by_question["q1"]["correct"] is True
    assert by_question["q1"]["score"] == 5.0
    assert by_question["q2"]["correct"] is False
    assert by_question["q2"]["score"] == 0.0
    # 答案来源披露（pi 审查 🟡6）：随审计链可查
    assert by_question["q1"]["answer_source"] == "provided"
    assert by_question["q2"]["answer_source"] == "generated"


def test_objective_schema_rejects_invalid_items() -> None:
    with pytest.raises(ValidationError):
        _ObjectiveItemInput(
            question_id="q1",
            standard_answer="",  # 标准答案不得为空
            student_answer="A",
            max_score=5,
        )
    with pytest.raises(ValidationError):
        _ObjectiveItemInput(
            question_id="q1",
            standard_answer="A",
            student_answer="A",
            max_score=0,  # 满分必须为正
        )


def test_grading_schema_rejects_score_above_max() -> None:
    with pytest.raises(ValidationError):
        _GradingInput(
            items=[
                {
                    "question_id": "q1",
                    "score": 11,
                    "max_score": 10,
                    "feedback": "超分",
                }
            ]
        )


# ── 审查 S5：批改工具角色守卫（计划 P2 验收条款，仅 evaluator 可调）──


class _NoopModel:
    """只用于构建图，不发起模型调用。"""

    def bind_tools(self, tools: Sequence[object]) -> _NoopModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content="unused")


@pytest.mark.parametrize(
    "tool_name,args",
    [
        (
            "grade_objective_answers",
            {
                "items": [
                    {
                        "question_id": "q1",
                        "standard_answer": "A",
                        "student_answer": "A",
                        "max_score": 5,
                    }
                ]
            },
        ),
        (
            "submit_grading",
            {"items": [{"question_id": "q1", "score": 5, "max_score": 5}]},
        ),
    ],
)
def test_grading_tools_reject_non_evaluator_roles(
    tool_name: str, args: dict[str, object]
) -> None:
    """supervisor / 助教 / 助学调用批改工具 → TOOL_UNAUTHORIZED；
    evaluator 可调（对照）——与 search_knowledge 的未授权断言同型。"""
    graph = CollaborativeAgentGraph(model=_NoopModel())

    for role in (
        AgentRole.SUPERVISOR,
        AgentRole.TEACHING_ASSISTANT,
        AgentRole.LEARNING_ASSISTANT,
    ):
        execution = ToolExecutor(graph.registry).execute(
            {"name": tool_name, "args": args, "id": f"unauthorized-{role.value}"},
            role,
        )
        assert execution.result.error_code is ErrorCode.TOOL_UNAUTHORIZED

    authorized = ToolExecutor(graph.registry).execute(
        {"name": tool_name, "args": args, "id": "authorized-evaluator"},
        AgentRole.EVALUATOR,
    )
    assert authorized.result.success is True


# ── handoff 模式批改链路 ──────────────────────────────────


def test_handoff_mode_grading_writes_channel_event_and_message(
    store: LearningRecordStore,
) -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(_handoff_responses()),
        checkpointer=InMemorySaver(),
        learning_store=store,
    )

    result = graph.run("请批改我的作业", "grading-handoff", user_id="student-a")

    grading = result["grading"]
    assert grading is not None
    assert len(grading.items) == 3
    # 总分由核心侧确定性汇总（不信任模型自报）
    assert grading.total_score == 15
    assert grading.max_total_score == 30
    # 事件脱敏：只带数字摘要
    events = _grading_events(result)
    assert len(events) == 1
    assert events[0].grading_item_count == 3
    assert events[0].grading_total_score == 15
    assert events[0].grading_max_total_score == 30
    # 消息元数据挂载（产出批改的作答消息，倒序口径同 chat.py references）
    graded = _last_message_with_grading(result)
    assert graded is not None
    assert message_grading(graded).total_score == 15  # type: ignore[union-attr]


def test_handoff_mode_grading_persists_all_questions(
    store: LearningRecordStore,
) -> None:
    """pi 三轮审查 🟡3：多题共享 tool_call_id，复合键下全部入库。"""
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(_handoff_responses()),
        checkpointer=InMemorySaver(),
        learning_store=store,
    )

    graph.run("请批改我的作业", "grading-persist", user_id="student-a")

    summary = store.summarize("student-a")
    assert summary["total_attempts"] == 3
    by_point = {p["knowledge_point"]: p for p in summary["knowledge_points"]}
    assert by_point["梯度下降"]["attempts"] == 2
    # 梯度下降：correct + incorrect → 正确率 0.5 < 0.6 → 预警
    assert summary["weak_points"] == ["梯度下降"]
    # q3 无知识点 → 未分类（总量有、聚合无）
    assert summary["uncategorized"]["attempts"] == 1


def test_grading_idempotent_on_replay_within_same_tool_call(
    store: LearningRecordStore,
) -> None:
    """同一 submit_grading 的重复落库（重放边界）被幂等键忽略。"""
    from core.graph_builder import _persist_grading_records
    from core.state import GradingItem, GradingResult

    grading = GradingResult(
        items=[
            GradingItem(question_id="q1", score=5, max_score=10),
            GradingItem(question_id="q2", score=10, max_score=10),
        ],
        overall_comment="",
        total_score=15,
        max_total_score=20,
    )

    _persist_grading_records(store, grading, "student-a", "s1", "call-x")
    _persist_grading_records(store, grading, "student-a", "s1", "call-x")

    assert store.summarize("student-a")["total_attempts"] == 2


def test_grading_without_store_does_not_break(
) -> None:
    """None 容忍：既有构造点不传 store，批改主链路不受影响。"""
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(_handoff_responses()),
        checkpointer=InMemorySaver(),
    )

    result = graph.run("请批改我的作业", "grading-no-store", user_id="student-a")

    assert result["grading"] is not None
    assert result["run_error"] is None


# ── tool 模式（生产）批改链路 ─────────────────────────────


def test_tool_mode_grading_flows_back_through_subagent_payload(
    store: LearningRecordStore,
) -> None:
    """pi 审查 🔴：生产 tool 模式下批改必须可见（_run_subagent 负载
    回传 + Supervisor 轮提取写通道），且幂等键随负载传递（🟡B）。"""
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(_tool_mode_responses()),
        checkpointer=InMemorySaver(),
        orchestration_mode="tool",
        learning_store=store,
    )

    result = graph.run("请批改我的作业", "grading-tool", user_id="student-a")

    grading = result["grading"]
    assert grading is not None
    assert grading.total_score == 15
    assert grading.max_total_score == 30
    # 落库同样生效（tool_call_id 随负载传递）
    assert store.summarize("student-a")["total_attempts"] == 3
    # 消息元数据挂载（tool 模式在 Supervisor 轮提取，挂到最终回答）
    graded = _last_message_with_grading(result)
    assert graded is not None
    assert graded is result["messages"][-1]


# ── 每轮重置与历史保留 ────────────────────────────────────


def test_grading_resets_per_turn_but_history_messages_keep_metadata(
    store: LearningRecordStore,
) -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel(
            [
                *_handoff_responses(),
                # 第二轮：直接回答，无批改
                AIMessage(content="你好，有什么可以帮你？"),
            ]
        ),
        checkpointer=InMemorySaver(),
        learning_store=store,
    )

    first = graph.run("请批改我的作业", "grading-reset", user_id="student-a")
    second = graph.run("你好", "grading-reset", user_id="student-a")

    assert first["grading"] is not None
    assert second["grading"] is None
    # 历史里的批改消息元数据仍在（刷新恢复的数据基础，pi 审查 🟡4）
    graded_messages = [
        message
        for message in second["messages"]
        if message_grading(message) is not None
    ]
    assert len(graded_messages) == 1
    assert message_grading(graded_messages[0]).total_score == 15  # type: ignore[union-attr]
